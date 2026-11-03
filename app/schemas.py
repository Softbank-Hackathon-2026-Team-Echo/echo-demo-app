"""API 응답 모양. JSON 키는 camelCase 다."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.ledger import LedgerSummary


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthResponse(CamelModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(CamelModel):
    status: Literal["ready", "unavailable"]


class InfoResponse(CamelModel):
    environment: str
    hostname: str
    version: str


class WriteResponse(CamelModel):
    n: int
    environment: str
    hostname: str
    wrote_at: datetime


class ErrorResponse(CamelModel):
    error: str


class EnvironmentCountResponse(CamelModel):
    environment: str
    count: int
    min_n: int
    max_n: int


class RecentWriteResponse(CamelModel):
    n: int
    environment: str
    wrote_at: datetime


class SummaryResponse(CamelModel):
    environment: str
    total: int
    distinct: int
    min_n: int | None
    max_n: int | None
    missing: int
    duplicates: int
    by_environment: list[EnvironmentCountResponse]
    recent: list[RecentWriteResponse]

    @classmethod
    def from_summary(cls, summary: LedgerSummary, environment: str) -> "SummaryResponse":
        return cls(
            environment=environment,
            total=summary.total,
            distinct=summary.distinct,
            min_n=summary.min_n,
            max_n=summary.max_n,
            missing=summary.missing,
            duplicates=summary.duplicates,
            by_environment=[
                EnvironmentCountResponse(
                    environment=e.environment, count=e.count, min_n=e.min_n, max_n=e.max_n
                )
                for e in summary.by_environment
            ],
            recent=[
                RecentWriteResponse(n=r.n, environment=r.environment, wrote_at=r.wrote_at)
                for r in summary.recent
            ],
        )
