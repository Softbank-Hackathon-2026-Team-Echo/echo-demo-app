"""순번 쓰기·장부 대조를 PgBouncer(transaction 모드)를 거친 실제 DB 로 확인한다."""

import asyncio

import psycopg
import pytest

from tests.support import make_settings, running_client

pytestmark = pytest.mark.integration

CONCURRENT_WRITES = 50


async def test_create_write_concurrent_50_numbers_are_contiguous_and_unique(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url)) as client:
        responses = await asyncio.gather(
            *(client.post("/api/writes") for _ in range(CONCURRENT_WRITES))
        )
        summary = (await client.get("/api/writes/summary")).json()

    assert [r.status_code for r in responses] == [201] * CONCURRENT_WRITES
    numbers = sorted(r.json()["n"] for r in responses)
    assert numbers == list(range(1, CONCURRENT_WRITES + 1))
    assert {r.json()["environment"] for r in responses} == {"aws-tokyo"}

    assert summary["total"] == CONCURRENT_WRITES
    assert summary["distinct"] == CONCURRENT_WRITES
    assert (summary["minN"], summary["maxN"]) == (1, CONCURRENT_WRITES)
    assert summary["missing"] == 0
    assert summary["duplicates"] == 0
    assert summary["byEnvironment"] == [
        {"environment": "aws-tokyo", "count": CONCURRENT_WRITES, "minN": 1, "maxN": 50}
    ]

    rows = empty_ledger.execute(
        "SELECT count(*), count(DISTINCT n), min(n), max(n) FROM demo.seq_write"
        " WHERE written_env = 'aws-tokyo'"
    ).fetchone()
    assert rows == (CONCURRENT_WRITES, CONCURRENT_WRITES, 1, CONCURRENT_WRITES)


async def test_create_write_response_shape_is_camel_case(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url)) as client:
        response = await client.post("/api/writes")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"n", "environment", "hostname", "wroteAt"}
    assert body["n"] == 1
    assert body["environment"] == "aws-tokyo"


async def test_create_write_continues_after_existing_max(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    # 다른 환경이 쓴 장부(복제로 넘어온 행)를 이어 받는 모습.
    empty_ledger.execute(
        "INSERT INTO demo.seq_write (n, written_env) VALUES (40, 'aws-tokyo'), (41, 'aws-tokyo')"
    )
    async with running_client(make_settings(database_url, echo_environment="gcp-seoul")) as client:
        body = (await client.post("/api/writes")).json()

    assert body["n"] == 42
    assert body["environment"] == "gcp-seoul"


async def test_summary_matches_inserted_rows_with_gap(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    # 4 를 일부러 비운다. 같은 번호는 UNIQUE 라 넣을 수 없다(duplicates 는 0).
    empty_ledger.execute(
        "INSERT INTO demo.seq_write (n, written_env) VALUES"
        " (1, 'aws-tokyo'), (2, 'aws-tokyo'), (3, 'aws-tokyo'), (5, 'gcp-seoul'), (6, 'gcp-seoul')"
    )
    async with running_client(make_settings(database_url, echo_environment="gcp-seoul")) as client:
        response = await client.get("/api/writes/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "gcp-seoul"
    assert body["total"] == 5
    assert body["distinct"] == 5
    assert (body["minN"], body["maxN"]) == (1, 6)
    assert body["missing"] == 1
    assert body["duplicates"] == 0
    assert body["byEnvironment"] == [
        {"environment": "aws-tokyo", "count": 3, "minN": 1, "maxN": 3},
        {"environment": "gcp-seoul", "count": 2, "minN": 5, "maxN": 6},
    ]
    assert [r["n"] for r in body["recent"]] == [6, 5, 3, 2, 1]
    assert body["recent"][0]["environment"] == "gcp-seoul"
    assert "wroteAt" in body["recent"][0]


async def test_summary_empty_ledger_returns_zeroes(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url)) as client:
        body = (await client.get("/api/writes/summary")).json()

    assert body["total"] == 0
    assert body["minN"] is None and body["maxN"] is None
    assert body["missing"] == 0 and body["duplicates"] == 0
    assert body["byEnvironment"] == [] and body["recent"] == []


async def test_summary_recent_is_limited_to_10(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    empty_ledger.execute("INSERT INTO demo.seq_write (n) SELECT g FROM generate_series(1, 15) AS g")
    async with running_client(make_settings(database_url)) as client:
        body = (await client.get("/api/writes/summary")).json()

    assert [r["n"] for r in body["recent"]] == list(range(15, 5, -1))
    # written_env 를 주지 않은 행(PoC writer.sh)은 기본값 unknown 이다.
    assert body["byEnvironment"] == [{"environment": "unknown", "count": 15, "minN": 1, "maxN": 15}]


async def test_readyz_with_db_returns_ready(database_url: str) -> None:
    async with running_client(make_settings(database_url)) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_metrics_counts_successful_writes_as_201(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url)) as client:
        for _ in range(3):
            assert (await client.post("/api/writes")).status_code == 201
        text = (await client.get("/metrics")).text

    assert 'http_requests_total{method="POST",path="/api/writes",status="201"} 3.0' in text
    assert 'http_request_duration_seconds_count{method="POST",path="/api/writes"} 3.0' in text
