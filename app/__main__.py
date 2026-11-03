"""컨테이너 진입점: `python -m app`. 포트는 PORT 환경 변수(기본 8080)."""

import uvicorn

from app.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.port,
        # 로그는 앱의 JSON 한 줄 형식을 쓴다. 접근 로그는 읽기 부하(50 rps)로 넘쳐 끈다.
        log_config=None,
        access_log=False,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
