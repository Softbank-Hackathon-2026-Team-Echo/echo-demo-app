"""DB 가 없을 때도 프로세스가 뜨고, 생존 확인만 성공하는지 본다."""

import re
from collections.abc import AsyncIterator

import httpx
import pytest

from app.ledger import LedgerSummary
from tests.support import make_settings, running_client

# 아무것도 듣지 않는 포트. 연결은 바로 거절된다.
UNREACHABLE_DATABASE_URL = "postgresql://demo:demo@127.0.0.1:1/demo?connect_timeout=1"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        UNREACHABLE_DATABASE_URL, db_pool_timeout_seconds=0.3, readiness_timeout_seconds=0.5
    )
    async with running_client(settings) as c:
        yield c


async def test_healthz_without_db_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


async def test_readyz_without_db_returns_unavailable(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_create_write_without_db_returns_503(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/writes")
    assert response.status_code == 503
    assert response.json() == {"error": "DB_UNAVAILABLE"}


async def test_summary_without_db_returns_503(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/writes/summary")
    assert response.status_code == 503
    assert response.json() == {"error": "DB_UNAVAILABLE"}


async def test_info_returns_environment_hostname_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/info")
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "aws-tokyo"
    assert body["version"] == "test"
    assert isinstance(body["hostname"], str) and body["hostname"]


async def test_info_blank_environment_returns_unknown() -> None:
    settings = make_settings(
        UNREACHABLE_DATABASE_URL, echo_environment="  ", app_version="", db_pool_timeout_seconds=0.3
    )
    async with running_client(settings) as client:
        body = (await client.get("/api/info")).json()
    assert body["environment"] == "unknown"
    assert body["version"] == "dev"


async def test_index_serves_page_without_external_resources(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for text in (
        "지금 순번",
        "응답 환경",
        "쓰기 시작",
        "멈춤",
        "/api/writes",
        "/api/writes/summary",
    ):
        assert text in html
    # 외부 CDN·폰트·스크립트를 쓰지 않는다(차단 환경에서도 뜨게).
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html)
    assert not re.search(r"""@import|url\(\s*["']?(https?:)?//""", html)


async def test_index_zero_downtime_client_values(client: httpx.AsyncClient) -> None:
    """무중단(#23): 긴 타임아웃·같은 키 재시도·지연 표시가 화면에 들어 있다."""
    html = (await client.get("/")).text
    for text in (
        "const WRITE_TIMEOUT_MS = 60000;",
        "const SUMMARY_TIMEOUT_MS = 30000;",
        "const RETRY_WINDOW_MS = 60000;",
        "const RETRY_EVERY_MS = 1000;",
        "new Set([502, 503, 504])",
        '"Idempotency-Key": key',
        'params.get("slow")',
        ": 3000;",  # ?slow 기본 임계 3000ms
        ": 1000;",  # ?interval 기본 1000ms(#24)
        "지연된 요청",
        "최대 지연(초)",
        "재시도 중 ",
        'id="s-slow"',
        'id="s-max"',
        "#strip span.slow",
    ):
        assert text in html, text


def test_ledger_summary_missing_counts_gaps_like_verify_sh() -> None:
    summary = LedgerSummary(total=5, distinct=5, min_n=1, max_n=7, by_environment=[], recent=[])
    assert summary.missing == 2
    assert summary.duplicates == 0


def test_ledger_summary_empty_ledger_has_no_missing() -> None:
    summary = LedgerSummary(
        total=0, distinct=0, min_n=None, max_n=None, by_environment=[], recent=[]
    )
    assert summary.missing == 0
    assert summary.duplicates == 0
