from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import Settings
from app.main import create_app


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": database_url,
        "echo_environment": "aws-tokyo",
        "app_version": "test",
        "db_pool_timeout_seconds": 5.0,
        # 붙잡기 기본(20초)은 시험을 느리게 하므로 짧게 둔다. 붙잡기 시험은 따로 넘긴다.
        "write_hold_seconds": 0.5,
        "write_hold_interval_seconds": 0.05,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@asynccontextmanager
async def running_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """앱 lifespan(풀 열기·스키마 맞춤)까지 돌린 뒤 ASGI 로 직접 부르는 클라이언트."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app.test") as client:
            yield client
