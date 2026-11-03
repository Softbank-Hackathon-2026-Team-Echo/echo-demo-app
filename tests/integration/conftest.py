"""통합 시험: PgBouncer(transaction 모드)를 거친 실제 PostgreSQL 에 붙는다.

- TEST_DATABASE_URL: PgBouncer 주소(로컬은 compose 의 127.0.0.1:56432). 시험이 장부를 비운다.
- REQUIRE_DB_TESTS=1(CI): DB 가 없거나 닿지 않으면 건너뛰지 않고 실패한다.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest

from app.db import read_schema_sql

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
REQUIRE_DB_TESTS_ENV = "REQUIRE_DB_TESTS"


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=3)


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRE_DB_TESTS_ENV) == "1":
        pytest.fail(f"{REQUIRE_DB_TESTS_ENV}=1 인데 {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        _unavailable(f"{TEST_DATABASE_URL_ENV} 가 없다 (docker compose up -d --wait db pgbouncer)")
    try:
        with _connect(url) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        _unavailable(f"{TEST_DATABASE_URL_ENV} 에 연결하지 못했다: {type(exc).__name__}")
    return url


@pytest.fixture
def admin_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with _connect(database_url) as conn:
        yield conn


@pytest.fixture
def empty_ledger(admin_conn: psycopg.Connection) -> psycopg.Connection:
    """스키마를 맞추고 장부를 비운다. 시험이 직접 행을 넣을 때 쓰는 연결을 돌려준다."""
    admin_conn.execute(read_schema_sql().encode())
    admin_conn.execute("TRUNCATE demo.seq_write RESTART IDENTITY")
    return admin_conn
