"""쓰기 붙잡기(#23 a안): 25006·연결 오류·풀 대기 초과면 503 대신 다시 시도한다. DB 없이 본다."""

import time
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from app import ledger
from app.config import Settings
from app.write_hold import hold_retry, is_retryable
from tests.support import make_settings, running_client
from tests.test_app_without_db import UNREACHABLE_DATABASE_URL


def _frozen() -> psycopg.Error:
    exc = psycopg.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction")
    assert exc.sqlstate == "25006"
    return exc


def test_defaults_hold_20s_pool_20s_and_idempotent_on() -> None:
    settings = Settings(_env_file=None, database_url="postgresql://x@127.0.0.1:1/x")
    assert settings.idempotent_writes is True
    assert settings.db_pool_timeout_seconds == 20.0
    assert settings.write_hold_seconds == 20.0
    assert settings.write_hold_interval_seconds == 0.25


@pytest.mark.parametrize(
    ("exc", "idempotent", "expected"),
    [
        (PoolTimeout("pool timeout"), False, True),
        (_frozen(), False, True),
        (psycopg.OperationalError("server closed the connection"), True, True),
        # 키 없는 쓰기는 커밋 여부를 모르는 연결 끊김을 다시 보내지 않는다(중복 방지).
        (psycopg.OperationalError("server closed the connection"), False, False),
        (psycopg.errors.UniqueViolation("dup"), True, False),
    ],
)
def test_is_retryable(exc: psycopg.Error, idempotent: bool, expected: bool) -> None:
    assert is_retryable(exc, idempotent=idempotent) is expected


async def test_hold_retry_25006_then_success_returns_result() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise _frozen()
        return "ok"

    started = time.monotonic()
    result = await hold_retry(attempt, idempotent=False, hold_seconds=2.0, interval_seconds=0.05)
    assert result == "ok"
    assert calls == 4
    assert time.monotonic() - started >= 3 * 0.05


async def test_hold_retry_gives_up_after_hold_seconds() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise _frozen()

    started = time.monotonic()
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await hold_retry(attempt, idempotent=True, hold_seconds=0.3, interval_seconds=0.05)
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 0.6
    assert 4 <= calls <= 8


async def test_hold_retry_non_retryable_raises_at_once() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise psycopg.errors.UniqueViolation("dup")

    with pytest.raises(psycopg.errors.UniqueViolation):
        await hold_retry(attempt, idempotent=True, hold_seconds=5.0, interval_seconds=0.05)
    assert calls == 1


async def test_hold_retry_zero_hold_tries_once() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise _frozen()

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await hold_retry(attempt, idempotent=True, hold_seconds=0, interval_seconds=0.25)
    assert calls == 1


async def test_create_write_25006_twice_then_201(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_insert(
        pool: object, environment: str, request_id: str
    ) -> ledger.IdempotentWrite:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _frozen()
        row = ledger.WrittenRow(n=7, wrote_at=datetime(2026, 11, 8, tzinfo=UTC))
        return ledger.IdempotentWrite(row=row, environment=environment, created=True)

    monkeypatch.setattr(ledger, "insert_next_idempotent", fake_insert)
    settings = make_settings(
        UNREACHABLE_DATABASE_URL,
        auto_migrate=False,
        db_pool_timeout_seconds=0.2,
        write_hold_seconds=2.0,
    )
    async with running_client(settings) as client:
        response = await client.post("/api/writes", headers={"Idempotency-Key": "k-hold"})

    assert response.status_code == 201
    assert response.json()["n"] == 7
    assert calls == 3


async def test_create_write_pool_wait_returns_503_after_hold() -> None:
    """풀 대기 초과가 이어지면 붙잡는 시간(1초)을 다 쓴 뒤 503. 그 전에는 끊지 않는다."""
    settings = make_settings(
        UNREACHABLE_DATABASE_URL,
        db_pool_timeout_seconds=0.2,
        write_hold_seconds=1.0,
        write_hold_interval_seconds=0.1,
    )
    async with running_client(settings) as client:
        started = time.monotonic()
        response = await client.post("/api/writes", headers={"Idempotency-Key": "k-pool"})
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"error": "DB_UNAVAILABLE"}
    # 마지막 시도가 시작될 수 있는 시각(≤1.0초) + 그 시도의 풀 대기(0.2초) + 여유.
    assert 0.9 <= elapsed < 1.0 + 0.2 + 0.6


async def test_create_write_hold_zero_returns_503_after_one_pool_wait() -> None:
    settings = make_settings(
        UNREACHABLE_DATABASE_URL, db_pool_timeout_seconds=0.2, write_hold_seconds=0
    )
    async with running_client(settings) as client:
        started = time.monotonic()
        response = await client.post("/api/writes")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert elapsed < 0.2 + 0.5
