from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UNKNOWN_ENVIRONMENT = "unknown"


class Settings(BaseSettings):
    """환경 변수로 받는 설정. 접속 문자열은 로그·응답에 내보내지 않는다(SecretStr)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PgBouncer(transaction 모드) 접속 문자열. libpq 형식(postgresql://…).
    database_url: SecretStr
    echo_environment: str = UNKNOWN_ENVIRONMENT
    app_version: str = "dev"
    auto_migrate: bool = True
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"

    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    # 풀에서 연결을 기다리는 최대 시간. 넘으면 쓰기는 503 이다.
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    # /readyz 의 SELECT 1 제한 시간.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    @field_validator("echo_environment", "app_version")
    @classmethod
    def _blank_to_default(cls, value: str) -> str:
        return value.strip()

    @property
    def environment(self) -> str:
        return self.echo_environment or UNKNOWN_ENVIRONMENT

    @property
    def version(self) -> str:
        return self.app_version or "dev"
