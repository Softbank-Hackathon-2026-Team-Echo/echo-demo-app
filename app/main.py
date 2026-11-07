import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from psycopg_pool import AsyncConnectionPool

from app import ledger
from app.config import Settings
from app.db import SchemaState, check_ready, create_pool
from app.json_logging import configure_logging
from app.metrics import HttpMetrics, MetricsMiddleware
from app.schemas import (
    ErrorResponse,
    HealthResponse,
    InfoResponse,
    ReadinessResponse,
    SummaryResponse,
    WriteResponse,
)

logger = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"
DB_UNAVAILABLE = "DB_UNAVAILABLE"
REQUEST_ID_TOO_LONG = "REQUEST_ID_TOO_LONG"
MAX_REQUEST_ID_LENGTH = 200


def _request_id(request: Request) -> str | None:
    """멱등 키: Idempotency-Key, 없으면 진입점 Envoy 가 붙이는 x-request-id. 둘 다 없으면 None."""
    for header in ("idempotency-key", "x-request-id"):
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True, slots=True)
class AppContext:
    settings: Settings
    pool: AsyncConnectionPool
    schema: SchemaState
    hostname: str


def _context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context


def _db_unavailable(action: str, exc: Exception) -> JSONResponse:
    logger.warning(
        "database request failed",
        extra={
            "action": action,
            "error_type": type(exc).__name__,
            "sqlstate": getattr(exc, "sqlstate", None),
        },
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(error=DB_UNAVAILABLE).model_dump(by_alias=True),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    # DATABASE_URL 은 필수다. 값이 없으면 여기서 설정 오류로 멈춘다.
    resolved = settings or Settings()
    configure_logging(resolved.log_level, resolved.environment)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(resolved)
        # DB 가 없어도 프로세스는 뜬다: 연결은 풀이 뒤에서 계속 시도한다.
        await pool.open(wait=False)
        schema = SchemaState(pool, enabled=resolved.auto_migrate)
        app.state.context = AppContext(
            settings=resolved, pool=pool, schema=schema, hostname=socket.gethostname()
        )
        logger.info(
            "app started",
            extra={"action": "startup", "version": resolved.version, "port": resolved.port},
        )
        await schema.try_at_startup(resolved.db_pool_timeout_seconds)
        try:
            yield
        finally:
            await pool.close()
            logger.info("app stopped", extra={"action": "shutdown"})

    app = FastAPI(
        title="echo-demo-app",
        version=resolved.version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 컷오버 중 어느 환경이 답했는지가 중요하므로 앞단·브라우저가 응답을 재사용하지 않게 한다.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    # 마지막에 더한 미들웨어가 가장 바깥이다 → no-store 를 포함한 모든 처리 시간을 잰다.
    metrics = HttpMetrics()
    app.add_middleware(MetricsMiddleware, metrics=metrics)

    @app.get("/healthz")
    async def healthz() -> HealthResponse:
        # 프로세스 생존만 본다. DB 를 보면 컷오버 중 PgBouncer PAUSE 동안 파드가 재시작된다.
        return HealthResponse()

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz(request: Request) -> JSONResponse:
        ctx = _context(request)
        ready = await check_ready(ctx.pool, ctx.settings.readiness_timeout_seconds)
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ReadinessResponse(status="ready" if ready else "unavailable").model_dump(),
        )

    @app.get("/api/info")
    async def info(request: Request) -> InfoResponse:
        ctx = _context(request)
        return InfoResponse(
            environment=ctx.settings.environment,
            hostname=ctx.hostname,
            version=ctx.settings.version,
        )

    @app.post(
        "/api/writes",
        status_code=status.HTTP_201_CREATED,
        response_model=WriteResponse,
        responses={
            200: {"model": WriteResponse, "description": "같은 멱등 키로 이미 쓴 요청"},
            400: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def create_write(request: Request) -> WriteResponse | JSONResponse:
        ctx = _context(request)
        request_id = _request_id(request) if ctx.settings.idempotent_writes else None
        if request_id is not None and len(request_id) > MAX_REQUEST_ID_LENGTH:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(error=REQUEST_ID_TOO_LONG).model_dump(by_alias=True),
            )
        try:
            await ctx.schema.ensure()
            if request_id is None:
                row = await ledger.insert_next(ctx.pool, ctx.settings.environment)
            else:
                written = await ledger.insert_next_idempotent(
                    ctx.pool, ctx.settings.environment, request_id
                )
        except psycopg.Error as exc:
            return _db_unavailable("seq_write", exc)
        if request_id is None:
            return WriteResponse(
                n=row.n,
                environment=ctx.settings.environment,
                hostname=ctx.hostname,
                wrote_at=row.wrote_at,
            )
        body = WriteResponse(
            n=written.row.n,
            environment=written.environment,
            hostname=ctx.hostname,
            wrote_at=written.row.wrote_at,
        )
        if written.created:
            return body
        # 같은 키로 다시 온 요청: 새로 쓰지 않고 처음 결과를 200 으로 돌려준다
        # (재생 스크립트가 201 과 구분, 인프라 12.4).
        return JSONResponse(
            status_code=status.HTTP_200_OK, content=body.model_dump(by_alias=True, mode="json")
        )

    @app.get(
        "/api/writes/summary",
        response_model=SummaryResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def writes_summary(request: Request) -> SummaryResponse | JSONResponse:
        ctx = _context(request)
        try:
            await ctx.schema.ensure()
            summary = await ledger.read_summary(ctx.pool)
        except psycopg.Error as exc:
            return _db_unavailable("seq_summary", exc)
        return SummaryResponse.from_summary(summary, ctx.settings.environment)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        # 클러스터 OTel 수집기(echo-infra 인프라 9)가 긁는다. DB 를 보지 않는다.
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8"

    return app
