"""`/metrics` 가 요청 수·처리 시간을 경로 템플릿 라벨로 내보내는지 본다(DB 없이)."""

import re
from collections.abc import AsyncIterator

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.metrics import DURATION_BUCKETS, UNMATCHED_PATH
from tests.support import make_settings, running_client

UNREACHABLE_DATABASE_URL = "postgresql://demo:***@127.0.0.1:1/demo?connect_timeout=1"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        UNREACHABLE_DATABASE_URL, db_pool_timeout_seconds=0.3, readiness_timeout_seconds=0.5
    )
    async with running_client(settings) as c:
        yield c


def _samples(text: str, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    """지표 이름 하나의 표본을 {정렬한 라벨: 값} 으로 모은다."""
    found: dict[tuple[tuple[str, str], ...], float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name:
                found[tuple(sorted(sample.labels.items()))] = sample.value
    return found


def _key(**labels: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


async def test_metrics_without_db_returns_prometheus_text(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert "# TYPE http_requests_total counter" in response.text
    assert "# TYPE http_request_duration_seconds histogram" in response.text


async def test_metrics_counts_requests_by_route_template_and_status(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/healthz")
    await client.get("/healthz")
    await client.post("/api/writes")  # DB 없음 → 503 (순번 쓰기 경로)
    await client.get("/readyz")  # DB 없음 → 503

    text = (await client.get("/metrics")).text
    total = _samples(text, "http_requests_total")
    assert total[_key(method="GET", path="/healthz", status="200")] == 2.0
    assert total[_key(method="POST", path="/api/writes", status="503")] == 1.0
    assert total[_key(method="GET", path="/readyz", status="503")] == 1.0


async def test_metrics_unknown_paths_share_one_label(client: httpx.AsyncClient) -> None:
    for path in ("/wp-login.php", "/api/writes/123", "/x/y/z?q=1"):
        assert (await client.get(path)).status_code == 404

    text = (await client.get("/metrics")).text
    total = _samples(text, "http_requests_total")
    assert total[_key(method="GET", path=UNMATCHED_PATH, status="404")] == 3.0
    # 날 경로가 라벨로 새지 않는다(카디널리티가 route 수로 고정).
    assert "wp-login" not in text
    assert "/api/writes/123" not in text
    paths = {dict(labels)["path"] for labels in total}
    assert paths <= {"/healthz", "/readyz", "/metrics", "/api/writes", UNMATCHED_PATH}


async def test_metrics_duration_histogram_has_fixed_buckets(client: httpx.AsyncClient) -> None:
    await client.get("/api/info")

    text = (await client.get("/metrics")).text
    buckets = _samples(text, "http_request_duration_seconds_bucket")
    les = sorted(
        float(dict(labels)["le"])
        for labels in buckets
        if dict(labels)["path"] == "/api/info" and dict(labels)["le"] != "+Inf"
    )
    assert les == list(DURATION_BUCKETS)
    count = _samples(text, "http_request_duration_seconds_count")
    assert count[_key(method="GET", path="/api/info")] == 1.0
    # 처리 시간 지표에는 응답 코드 라벨을 두지 않는다(버킷 수 × 코드 수로 늘지 않게).
    assert not re.search(r"http_request_duration_seconds_bucket\{[^}]*status=", text)


async def test_metrics_registries_are_separate_per_app() -> None:
    # 앱을 두 번 만들어도 전역 레지스트리 중복 등록 오류가 나지 않고 숫자가 섞이지 않는다.
    settings = make_settings(UNREACHABLE_DATABASE_URL, db_pool_timeout_seconds=0.3)
    async with running_client(settings) as first:
        await first.get("/healthz")
    async with running_client(settings) as second:
        text = (await second.get("/metrics")).text
    total = _samples(text, "http_requests_total")
    assert _key(method="GET", path="/healthz", status="200") not in total
