# 인프라 9: 시연 앱 Prometheus `/metrics`

- 상태: Draft (2026-11-05, 지민 결정 "로컬에서 최대한 잘 테스트해보고 올려")
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/9 (담당 SeoDongOk, 구현 인수 댓글 게시됨)
- 바꾸는 SPEC: [infra-4-demo-app.md](infra-4-demo-app.md) 4절(인터페이스)에 `GET /metrics` 를 더한다. 다르면 이 문서가 우선한다.
- 짝 PR: echo-infra `feat/otel-collector` (클러스터마다 OTel 수집기 → 진입점 Prometheus remote write, SPEC `specs/infra-9-otel-collector.md`)

## 1. 목적

인프라 9 완료 기준 "모든 환경이 같은 OTel 수집기로 요청 수·오류율·지연을 보낸다" 를 채우려면, 세 환경(aws-tokyo / gcp-seoul / onprem) 안에서 **같은 이름의 앱 지표**를 긁을 곳이 필요하다. 지금 앱에는 `/metrics` 가 없다(운영 온프레 앱 `GET /metrics` → 404, 11/5 확인). EKS 앞단은 ALB 라 클러스터 안에 요청 지표가 없어, 앱이 직접 내는 것이 세 환경을 같은 이름으로 맞추는 가장 짧은 길이다.

## 2. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | `prometheus_client` 로 `GET /metrics`(Prometheus 텍스트 형식)를 낸다. OTel SDK·OTLP 는 쓰지 않는다 — 수집기(Prometheus receiver)가 긁는다 | 계획 A안(`otel/plan.md`) |
| AD-1 | 지표 2개: `http_requests_total{method,path,status}`(Counter), `http_request_duration_seconds{method,path}`(Histogram, 버킷 0.005·0.01·0.025·0.05·0.1·0.25·0.5·1·2.5·5·10초 — 컷오버 쓰기 정지가 10초 안쪽에서 보이게) | AI-default |
| AD-2 | `path` 는 **경로 템플릿**(FastAPI route path). 일치하는 route 가 없으면 `unmatched` 하나. `/healthz`·`/readyz`·`/metrics` 도 같은 규칙으로 센다(고정 경로라 카디널리티가 늘지 않는다). 대시보드가 이 세 경로를 걸러 낸다 | AI-default |
| AD-3 | 처리 시간에는 `status` 라벨을 두지 않는다(버킷 × 코드 수로 늘지 않게). 오류율은 Counter 의 `status` 로 계산한다 | AI-default |
| AD-4 | 환경 라벨(`environment` 등)을 앱이 넣지 않는다. `cloud.provider`·`cloud.region`·`k8s.cluster.name` 은 클러스터 수집기가 붙인다(같은 이미지 digest 를 세 환경에 쓰므로) | AI-default |
| AD-5 | 레지스트리는 앱마다 따로(`CollectorRegistry`), `*_created` 표본은 끈다(remote write 시계열 수를 줄인다). 프로세스·GC 기본 지표는 내지 않는다 | AI-default |
| AD-6 | 미들웨어는 순수 ASGI(응답 본문 버퍼링 없음). 처리 중 예외는 500 으로 세고 예외는 그대로 올린다. `/metrics` 는 DB 를 보지 않는다 | AI-default |

## 3. 인터페이스

| 메서드·경로 | 응답 |
|---|---|
| `GET /metrics` | 200 `text/plain; version=…` Prometheus 텍스트. `Cache-Control: no-store`. 인증 없음(클러스터 안에서만 긁는다 — 공개 진입점 경로로도 열리지만 숫자만 있고 비밀값은 없다) |

예:

```text
http_requests_total{method="POST",path="/api/writes",status="201"} 412.0
http_requests_total{method="GET",path="unmatched",status="404"} 3.0
http_request_duration_seconds_bucket{le="0.05",method="POST",path="/api/writes"} 400.0
```

시계열 수 어림: route 7개(`/`·`/healthz`·`/readyz`·`/metrics`·`/api/info`·`/api/writes`·`/api/writes/summary`) + `unmatched` → Counter 는 (경로·메서드·코드) 실제 조합만(수십 개), Histogram 은 경로·메서드당 버킷 11 + `+Inf`·`_sum`·`_count` = 14개. 파드 하나에 200개 미만.

## 4. 완료 기준

1. `GET /metrics` 200 에 두 지표가 보이고, 순번 쓰기(`POST /api/writes`) 201·503 이 `path="/api/writes"` 로 센다. (시험 `tests/test_metrics.py`, `tests/integration/test_writes.py::test_metrics_counts_successful_writes_as_201`)
2. 없는 경로(`/wp-login.php`, `/api/writes/123`)는 `path="unmatched"` 하나로 묶이고 날 경로가 라벨로 새지 않는다.
3. 처리 시간 버킷이 AD-1 의 고정 목록이고 `status` 라벨이 없다.
4. 기존 시험 전부 통과(CI `test`: ruff·format·mypy·pytest 건너뜀 0·docker build·컨테이너 `/healthz`). 실행 이미지에 pip 가 없는 상태 유지(의존성은 builder 단계 `uv sync`).
5. 로컬 docker 컨테이너에서 `curl /metrics` 200 에 지표가 보인다.

## 5. 하지 않는 것

- 운영 세 환경 앱 이미지 교체(이 PR 병합 뒤 echo-infra 적용 절차서에서 한다), OTel SDK·트레이스, 인증.
- 진입점·수집기 설정(echo-infra PR 몫).
