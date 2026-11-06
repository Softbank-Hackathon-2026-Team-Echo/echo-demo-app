"""순번 장부(demo.seq_write) 쓰기·대조."""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

# 번호 매기기(max(n)+1)를 직렬화하는 트랜잭션 advisory lock 키. "ECHO"(0x4543484F) + 용도 번호 1.
# 트랜잭션 잠금이라 PgBouncer transaction 모드에서도 커밋·롤백 때 풀린다.
SEQ_WRITE_LOCK_KEY = 0x4543484F_0001

# 잠금을 쓰지 않는 다른 쓰기(PoC writer.sh 등)와 번호가 부딪히면 다시 시도한다.
_MAX_WRITE_ATTEMPTS = 3
_RECENT_LIMIT = 10

_INSERT_NEXT_SQL = """
INSERT INTO demo.seq_write (n, written_env)
SELECT COALESCE(max(n), 0) + 1, %s FROM demo.seq_write
RETURNING n, wrote_at
"""

# 멱등 쓰기(인프라 12.4). 같은 쓰기 잠금 안에서 찾고 없으면 넣으므로
# 같은 키가 동시에 와도 행은 하나다.
_FIND_BY_REQUEST_ID_SQL = """
SELECT n, wrote_at, written_env FROM demo.seq_write WHERE request_id = %s
"""

_INSERT_NEXT_WITH_REQUEST_ID_SQL = """
INSERT INTO demo.seq_write (n, written_env, request_id)
SELECT COALESCE(max(n), 0) + 1, %s, %s FROM demo.seq_write
RETURNING n, wrote_at
"""

_TOTALS_SQL = "SELECT count(*), count(DISTINCT n), min(n), max(n) FROM demo.seq_write"

_BY_ENVIRONMENT_SQL = """
SELECT written_env, count(*), min(n), max(n)
FROM demo.seq_write
GROUP BY written_env
ORDER BY min(n), written_env
"""

_RECENT_SQL = """
SELECT n, written_env, wrote_at
FROM demo.seq_write
ORDER BY n DESC
LIMIT %s
"""


@dataclass(frozen=True, slots=True)
class WrittenRow:
    n: int
    wrote_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotentWrite:
    row: WrittenRow
    environment: str
    created: bool


@dataclass(frozen=True, slots=True)
class EnvironmentCount:
    environment: str
    count: int
    min_n: int
    max_n: int


@dataclass(frozen=True, slots=True)
class RecentWrite:
    n: int
    environment: str
    wrote_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    total: int
    distinct: int
    min_n: int | None
    max_n: int | None
    by_environment: list[EnvironmentCount]
    recent: list[RecentWrite]

    @property
    def missing(self) -> int:
        """비어 있는 번호 수. PoC verify.sh 와 같은 뜻: (max − min + 1) − distinct."""
        if self.min_n is None or self.max_n is None:
            return 0
        return (self.max_n - self.min_n + 1) - self.distinct

    @property
    def duplicates(self) -> int:
        return self.total - self.distinct


async def insert_next(pool: AsyncConnectionPool, environment: str) -> WrittenRow:
    for attempt in range(1, _MAX_WRITE_ATTEMPTS + 1):
        try:
            async with pool.connection() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEQ_WRITE_LOCK_KEY,))
                cursor = await conn.execute(_INSERT_NEXT_SQL, (environment,))
                row = await cursor.fetchone()
        except psycopg.errors.UniqueViolation:
            if attempt == _MAX_WRITE_ATTEMPTS:
                raise
            continue
        if row is None:
            raise RuntimeError("INSERT … RETURNING returned no row")
        return WrittenRow(n=row[0], wrote_at=row[1])
    raise AssertionError("unreachable")


async def insert_next_idempotent(
    pool: AsyncConnectionPool, environment: str, request_id: str
) -> IdempotentWrite:
    """같은 request_id 가 이미 있으면 그 행을, 없으면 새 번호로 넣은 행을 돌려준다."""
    for attempt in range(1, _MAX_WRITE_ATTEMPTS + 1):
        try:
            async with pool.connection() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEQ_WRITE_LOCK_KEY,))
                cursor = await conn.execute(_FIND_BY_REQUEST_ID_SQL, (request_id,))
                found = await cursor.fetchone()
                if found is not None:
                    return IdempotentWrite(
                        row=WrittenRow(n=found[0], wrote_at=found[1]),
                        environment=found[2],
                        created=False,
                    )
                cursor = await conn.execute(
                    _INSERT_NEXT_WITH_REQUEST_ID_SQL, (environment, request_id)
                )
                row = await cursor.fetchone()
        except psycopg.errors.UniqueViolation:
            if attempt == _MAX_WRITE_ATTEMPTS:
                raise
            continue
        if row is None:
            raise RuntimeError("INSERT … RETURNING returned no row")
        return IdempotentWrite(
            row=WrittenRow(n=row[0], wrote_at=row[1]), environment=environment, created=True
        )
    raise AssertionError("unreachable")


async def read_summary(pool: AsyncConnectionPool) -> LedgerSummary:
    async with pool.connection() as conn, conn.transaction():
        # 세 조회가 같은 시점을 보게 한다(트랜잭션 안에서만 유효해 PgBouncer 에 안전).
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        totals = await (await conn.execute(_TOTALS_SQL)).fetchone()
        by_env_rows = await (await conn.execute(_BY_ENVIRONMENT_SQL)).fetchall()
        recent_rows = await (await conn.execute(_RECENT_SQL, (_RECENT_LIMIT,))).fetchall()
    if totals is None:
        raise RuntimeError("aggregate query returned no row")
    return LedgerSummary(
        total=totals[0],
        distinct=totals[1],
        min_n=totals[2],
        max_n=totals[3],
        by_environment=[
            EnvironmentCount(environment=r[0], count=r[1], min_n=r[2], max_n=r[3])
            for r in by_env_rows
        ],
        recent=[RecentWrite(n=r[0], environment=r[1], wrote_at=r[2]) for r in recent_rows],
    )
