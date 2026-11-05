"""Prometheus 지표 (`GET /metrics`).

세 환경 클러스터의 OTel 수집기(echo-infra 인프라 9)가 이 엔드포인트를 긁어
진입점 Prometheus 로 보낸다.
환경 구분(`cloud.provider` 등)은 수집기가 붙이므로 앱은 환경 라벨을 넣지 않는다.

- `http_requests_total{method,path,status}` (Counter)
- `http_request_duration_seconds{method,path}` (Histogram)

`path` 는 **경로 템플릿**(FastAPI route 의 path)이다. 일치하는 route 가 없는 요청(404 스캐너 등)은
`unmatched` 하나로 묶어 라벨 종류(카디널리티)가 route 수를 넘지 않게 한다.
"""

import time
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
)
from prometheus_client.exposition import generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UNMATCHED_PATH = "unmatched"

# `*_created` 표본은 쓰지 않는다. 켜 두면 라벨 묶음마다 시계열이 하나씩 더 생겨
# remote write 양만 는다. (prometheus_client 가 이 함수에 타입 힌트를 달지 않았다.)
disable_created_metrics()  # type: ignore[no-untyped-call]

# 컷오버 중 PgBouncer PAUSE 로 쓰기가 몇 초 기다릴 수 있어(화면 요청 제한 10초)
# 10초까지 버킷을 둔다.
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class HttpMetrics:
    """앱 하나가 쓰는 지표 묶음.

    시험에서 앱을 여러 번 만들어도 겹치지 않게 레지스트리를 앱마다 따로 둔다.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "http_requests_total",
            "HTTP 요청 수 (경로 템플릿·메서드·응답 코드별)",
            labelnames=("method", "path", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP 요청 처리 시간(초, 경로 템플릿·메서드별)",
            labelnames=("method", "path"),
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )

    def observe(self, method: str, path: str, status: int, seconds: float) -> None:
        self.requests.labels(method=method, path=path, status=str(status)).inc()
        self.duration.labels(method=method, path=path).observe(seconds)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def route_template(scope: Scope) -> str:
    """라우터가 고른 route 의 경로 템플릿. 없으면 `unmatched`."""
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_PATH


class MetricsMiddleware:
    """모든 HTTP 요청의 수·처리 시간을 센다(순수 ASGI — 응답 본문을 버퍼링하지 않는다)."""

    def __init__(self, app: ASGIApp, metrics: HttpMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 처리 중 예외가 나면 응답 코드는 500 으로 센다(예외는 그대로 위로 보낸다).
            self.metrics.observe(
                method=str(scope.get("method", "")),
                path=route_template(scope),
                status=status_code,
                seconds=time.perf_counter() - started,
            )
