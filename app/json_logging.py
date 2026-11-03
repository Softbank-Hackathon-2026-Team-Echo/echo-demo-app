import json
import logging
from datetime import UTC, datetime
from typing import Any

COMPONENT = "echo-demo-app"

# LogRecord 기본 속성. 이 밖의 키는 extra 로 넘긴 값이라 그대로 싣는다.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName", "color_message"}
)


class JsonFormatter(logging.Formatter):
    """로그 한 줄 = JSON 객체 하나. message 는 고정 문구, 값은 extra 로 싣는다."""

    def __init__(self, environment: str) -> None:
        super().__init__()
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "component": COMPONENT,
            "environment": self._environment,
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str, environment: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(environment))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
