"""PostgreSQL 연결 풀과 스키마 맞춤.

앱은 PgBouncer(transaction 모드)로만 DB 에 붙는다. 그래서
- 서버 측 prepared statement 를 쓰지 않는다(`prepare_threshold=None`).
- 세션 상태(SET, 세션 advisory lock, LISTEN)를 쓰지 않고, 잠금은 트랜잭션 잠금만 쓴다.
- 연결은 autocommit 으로 두고 트랜잭션은 `conn.transaction()` 블록으로만 연다.
"""

import asyncio
import logging
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import Settings

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

# 여러 앱 인스턴스가 동시에 스키마를 맞출 때 CREATE … IF NOT EXISTS 끼리 부딪히지 않게 한다.
# 값은 "ECHO"(0x4543484F) 뒤에 용도 번호를 붙였다. 쓰기 잠금은 app.ledger 에 있다.
SCHEMA_LOCK_KEY = 0x4543484F_0002


def create_pool(settings: Settings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout_seconds,
        kwargs={"autocommit": True, "prepare_threshold": None},
        # PgBouncer 가 다시 뜬 뒤 끊긴 연결을 내주지 않게 꺼낼 때 확인한다.
        check=AsyncConnectionPool.check_connection,
        name="echo-demo-app",
        open=False,
    )


def read_schema_sql() -> str:
    return SCHEMA_FILE.read_text(encoding="utf-8")


async def apply_schema(pool: AsyncConnectionPool) -> None:
    schema_sql = read_schema_sql()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
        # 매개변수가 없는 실행이라 여러 문장을 한 번에 보낼 수 있다.
        await conn.execute(schema_sql.encode())


class SchemaState:
    """시작 때 스키마 맞춤이 실패하면 DB 를 쓰는 첫 요청 전에 다시 시도한다."""

    def __init__(self, pool: AsyncConnectionPool, *, enabled: bool) -> None:
        self._pool = pool
        self._ready = not enabled
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._ready

    async def ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await apply_schema(self._pool)
            self._ready = True
            logger.info("schema ready", extra={"action": "schema_apply"})

    async def try_at_startup(self, timeout_seconds: float) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.ensure()
        except (psycopg.Error, TimeoutError) as exc:
            logger.warning(
                "schema apply failed at startup; will retry before first DB request",
                extra={"action": "schema_apply", "error_type": type(exc).__name__},
            )


async def check_ready(pool: AsyncConnectionPool, timeout_seconds: float) -> bool:
    try:
        async with asyncio.timeout(timeout_seconds), pool.connection() as conn:
            await conn.execute("SELECT 1")
    except (psycopg.Error, TimeoutError):
        return False
    return True
