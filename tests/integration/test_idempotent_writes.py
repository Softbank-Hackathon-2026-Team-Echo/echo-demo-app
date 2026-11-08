"""멱등 쓰기(인프라 12.4): 같은 요청 키로 다시 보내면 새 행 대신 처음 결과를 돌려준다."""

import asyncio

import psycopg
import pytest

from app.db import read_schema_sql
from tests.support import make_settings, running_client

pytestmark = pytest.mark.integration

CONCURRENT_SAME_KEY = 10


async def test_create_write_same_key_twice_returns_first_row_with_200(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        first = await client.post("/api/writes", headers={"Idempotency-Key": "k-1"})
        again = await client.post("/api/writes", headers={"Idempotency-Key": "k-1"})

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["n"] == first.json()["n"] == 1
    assert again.json()["wroteAt"] == first.json()["wroteAt"]
    assert set(again.json()) == {"n", "environment", "hostname", "wroteAt"}
    assert empty_ledger.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (1,)


async def test_create_write_same_key_concurrent_writes_one_row(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        responses = await asyncio.gather(
            *(
                client.post("/api/writes", headers={"Idempotency-Key": "same"})
                for _ in range(CONCURRENT_SAME_KEY)
            )
        )

    assert sorted(r.status_code for r in responses) == [200] * (CONCURRENT_SAME_KEY - 1) + [201]
    assert {r.json()["n"] for r in responses} == {1}
    assert empty_ledger.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (1,)


async def test_create_write_falls_back_to_x_request_id(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        first = await client.post("/api/writes", headers={"x-request-id": "envoy-1"})
        again = await client.post("/api/writes", headers={"x-request-id": "envoy-1"})

    assert (first.status_code, again.status_code) == (201, 200)
    row = empty_ledger.execute("SELECT request_id FROM demo.seq_write").fetchone()
    assert row == ("envoy-1",)


async def test_create_write_replay_keeps_order_and_skips_existing(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    """재생 흉내: 1·2·3 은 이미 있고 1~5 를 순서대로 다시 보내면 4·5 만 같은 번호로 들어간다."""
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        for i in range(1, 4):
            await client.post("/api/writes", headers={"Idempotency-Key": f"r-{i}"})
        replayed = [
            await client.post("/api/writes", headers={"Idempotency-Key": f"r-{i}"})
            for i in range(1, 6)
        ]

    assert [r.status_code for r in replayed] == [200, 200, 200, 201, 201]
    assert [r.json()["n"] for r in replayed] == [1, 2, 3, 4, 5]


async def test_create_write_key_too_long_returns_400_without_writing(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        response = await client.post("/api/writes", headers={"Idempotency-Key": "x" * 201})

    assert response.status_code == 400
    assert response.json() == {"error": "REQUEST_ID_TOO_LONG"}
    assert empty_ledger.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (0,)


async def test_create_write_without_key_is_unchanged_when_enabled(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    async with running_client(make_settings(database_url, idempotent_writes=True)) as client:
        responses = [await client.post("/api/writes") for _ in range(2)]

    assert [r.status_code for r in responses] == [201, 201]
    assert [r.json()["n"] for r in responses] == [1, 2]
    rows = empty_ledger.execute("SELECT count(*) FROM demo.seq_write WHERE request_id IS NULL")
    assert rows.fetchone() == (2,)


async def test_create_write_flag_off_ignores_key(
    database_url: str, empty_ledger: psycopg.Connection
) -> None:
    settings = make_settings(database_url, idempotent_writes=False)
    async with running_client(settings) as client:
        first = await client.post("/api/writes", headers={"Idempotency-Key": "k-1"})
        again = await client.post("/api/writes", headers={"Idempotency-Key": "k-1"})

    assert (first.status_code, again.status_code) == (201, 201)
    assert (first.json()["n"], again.json()["n"]) == (1, 2)
    rows = empty_ledger.execute("SELECT count(*) FROM demo.seq_write WHERE request_id IS NULL")
    assert rows.fetchone() == (2,)


def test_schema_sql_twice_is_safe(empty_ledger: psycopg.Connection) -> None:
    empty_ledger.execute(read_schema_sql().encode())
    empty_ledger.execute(read_schema_sql().encode())
    indexes = empty_ledger.execute(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'seq_write_request_id_key'"
    ).fetchone()
    assert indexes == (1,)
