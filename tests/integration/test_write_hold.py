"""쓰기 붙잡기(#23 a안)를 실제 DB(PgBouncer 경유)로 본다.

온프레 복귀 freeze 는 트리거로 흉내 낸다: 정한 시각까지 INSERT 를 SQLSTATE 25006 으로 거절한다.
"""

import time
from collections.abc import Iterator

import psycopg
import pytest

from tests.support import make_settings, running_client

pytestmark = pytest.mark.integration

_FREEZE_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS demo.test_freeze (until timestamptz NOT NULL);
TRUNCATE demo.test_freeze;
CREATE OR REPLACE FUNCTION demo.test_freeze_reject() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM demo.test_freeze WHERE until > clock_timestamp()) THEN
    RAISE EXCEPTION 'cannot execute INSERT in a read-only transaction' USING ERRCODE = '25006';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS test_freeze_reject ON demo.seq_write;
CREATE TRIGGER test_freeze_reject BEFORE INSERT ON demo.seq_write
  FOR EACH ROW EXECUTE FUNCTION demo.test_freeze_reject();
"""

_FREEZE_TEARDOWN_SQL = """
DROP TRIGGER IF EXISTS test_freeze_reject ON demo.seq_write;
DROP FUNCTION IF EXISTS demo.test_freeze_reject();
DROP TABLE IF EXISTS demo.test_freeze;
"""


@pytest.fixture
def freeze(empty_ledger: psycopg.Connection) -> Iterator[psycopg.Connection]:
    empty_ledger.execute(_FREEZE_SETUP_SQL.encode())
    try:
        yield empty_ledger
    finally:
        empty_ledger.execute(_FREEZE_TEARDOWN_SQL.encode())


def _freeze_for(conn: psycopg.Connection, seconds: float) -> None:
    conn.execute(
        "INSERT INTO demo.test_freeze (until)"
        " VALUES (clock_timestamp() + %s * interval '1 second')",
        (seconds,),
    )


async def test_create_write_held_through_25006_freeze_then_201_one_row(
    database_url: str, freeze: psycopg.Connection
) -> None:
    _freeze_for(freeze, 1.0)
    settings = make_settings(database_url, write_hold_seconds=5.0, write_hold_interval_seconds=0.25)
    async with running_client(settings) as client:
        started = time.monotonic()
        first = await client.post("/api/writes", headers={"Idempotency-Key": "fb-1"})
        elapsed = time.monotonic() - started
        again = await client.post("/api/writes", headers={"Idempotency-Key": "fb-1"})

    assert first.status_code == 201
    assert elapsed >= 0.7  # freeze(1초) 동안 붙잡혀 있었다
    assert again.status_code == 200
    assert again.json()["n"] == first.json()["n"] == 1
    assert freeze.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (1,)


async def test_create_write_without_key_held_through_freeze(
    database_url: str, freeze: psycopg.Connection
) -> None:
    _freeze_for(freeze, 0.6)
    settings = make_settings(database_url, write_hold_seconds=5.0, write_hold_interval_seconds=0.1)
    async with running_client(settings) as client:
        response = await client.post("/api/writes")

    assert response.status_code == 201
    assert freeze.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (1,)


async def test_create_write_freeze_longer_than_hold_returns_503_without_row(
    database_url: str, freeze: psycopg.Connection
) -> None:
    _freeze_for(freeze, 30.0)
    settings = make_settings(database_url, write_hold_seconds=0.8, write_hold_interval_seconds=0.1)
    async with running_client(settings) as client:
        started = time.monotonic()
        response = await client.post("/api/writes", headers={"Idempotency-Key": "fb-2"})
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"error": "DB_UNAVAILABLE"}
    assert 0.6 <= elapsed < 3.0
    assert freeze.execute("SELECT count(*) FROM demo.seq_write").fetchone() == (0,)
