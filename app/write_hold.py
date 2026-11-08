"""쓰기 붙잡기(#23 a안).

온프레 복귀(failback) 동안 DB 는 잠깐 쓰기를 받지 않는다: 읽기 전용 거절(SQLSTATE 25006),
PgBouncer·DB 재연결 중의 연결 오류, 풀 대기 초과. 이때 503 을 바로 돌려주지 않고 짧은 간격으로
다시 시도해, 컷오버(③)의 PgBouncer PAUSE 처럼 손님에게는 「느린 응답」으로만 보이게 한다.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import psycopg
from psycopg_pool import PoolTimeout

logger = logging.getLogger(__name__)

# 읽기 전용 트랜잭션에서 쓰기를 시도했다(failback freeze). 거절이라 아무것도 쓰이지 않았다.
READ_ONLY_SQL_TRANSACTION = "25006"


def is_retryable(exc: psycopg.Error, *, idempotent: bool) -> bool:
    """다시 보내도 중복 행이 생기지 않는 실패만 붙잡는다."""
    if isinstance(exc, PoolTimeout):
        # 연결을 못 받아 SQL 을 보내지 않았다.
        return True
    if exc.sqlstate == READ_ONLY_SQL_TRANSACTION:
        return True
    if isinstance(exc, psycopg.OperationalError):
        # 연결이 끊긴 경우는 커밋이 됐는지 모를 수 있다 → 멱등 키가 있을 때만 다시 보낸다
        # (키가 있으면 이미 쓴 행을 찾아 200 으로 돌려준다).
        return idempotent
    return False


async def hold_retry[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    idempotent: bool,
    hold_seconds: float,
    interval_seconds: float,
) -> T:
    """attempt 를 성공할 때까지 interval 간격으로 다시 부른다.

    hold_seconds 를 넘기면 마지막 예외를 그대로 올린다. 시간 제한은 「다음 시도를 시작할 수
    있는가」로만 본다. 한 시도 안의 풀 대기(db_pool_timeout_seconds)는 따로 걸리므로
    최악의 응답 시간은 대략 hold_seconds + db_pool_timeout_seconds 다.
    """
    started = time.monotonic()
    deadline = started + hold_seconds
    attempts = 0
    while True:
        attempts += 1
        try:
            result = await attempt()
        except psycopg.Error as exc:
            retryable = is_retryable(exc, idempotent=idempotent)
            if not retryable or time.monotonic() + interval_seconds > deadline:
                logger.warning(
                    "write hold gave up",
                    extra={
                        "action": "seq_write_hold",
                        "attempts": attempts,
                        "held_seconds": round(time.monotonic() - started, 3),
                        "retryable": retryable,
                        "error_type": type(exc).__name__,
                        "sqlstate": exc.sqlstate,
                    },
                )
                raise
            await asyncio.sleep(interval_seconds)
            continue
        if attempts > 1:
            logger.info(
                "write held then succeeded",
                extra={
                    "action": "seq_write_hold",
                    "attempts": attempts,
                    "held_seconds": round(time.monotonic() - started, 3),
                },
            )
        return result
