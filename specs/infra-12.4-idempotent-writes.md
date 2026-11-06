# 인프라 12.4: 시연 앱 멱등 쓰기 (`POST /api/writes`)

- 상태: 구현 (2026-11-06, 로컬 시험 33 통과 · 운영 적용 전)
- GitHub 이슈: (만들 예정) · 짝 SPEC echo-infra `specs/infra-12.4-request-journal.md`
- 바꾸는 SPEC: [infra-4-demo-app.md](infra-4-demo-app.md) 4절 `POST /api/writes` 에 요청 헤더 하나와 응답 200 한 가지를 **더한다**. 스위치가 꺼져 있으면 4절 그대로다.
- 작성: 이도현
- 진행 방식(11/5 회의): 사전 허락 없이 추가만, 끝나면 추가한 부분만 정리해서 공유

## 1. 목적

진입점이 기록해 둔 쓰기 요청을 승격된 서울 B 에 다시 보낼 때(echo-infra 12.4), **이미 B 에 있는 쓰기는 건너뛰고 빠진 쓰기만** 들어가야 한다. 지금 `POST /api/writes` 는 본문 `{}` 를 받고 `n = max(n)+1` 로 번호를 정해 같은 요청을 다시 보내면 새 행이 생긴다.

## 2. 결정

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | 스위치 `IDEMPOTENT_WRITES=false` 기본. 꺼져 있으면 헤더를 보지 않고 지금 경로(`ledger.insert_next`) 그대로 | 추가만 |
| D-2 | 키 = `Idempotency-Key` 헤더, 없으면 `x-request-id`(Envoy 가 붙임), 둘 다 없으면 지금 경로 | 진입점 기록과 같은 키 |
| D-3 | 칸 `demo.seq_write.request_id text NULL` + 부분 고유 색인 `WHERE request_id IS NOT NULL` | 기존 행·키 없는 쓰기에 영향 없음 |
| D-4 | 같은 키가 이미 있으면 **200** + 기존 행(`n`·`wrote_at`·`environment`)을 돌려준다. 새로 쓰면 지금처럼 201 | 재생 스크립트가 "새로 들어감 / 이미 있음"을 구분 |
| D-5 | 키는 최대 200자, 넘으면 400 | 색인 크기 제한 |
| D-6 | 기존 `insert_next`·`_INSERT_NEXT_SQL` 은 고치지 않고 새 함수 `insert_next_idempotent` 를 더한다 | diff 를 추가 위주로 |
| D-7 | 재생으로 들어간 행의 `written_env` 는 실제로 쓴 곳(`gcp-seoul`)이 된다 | 사실대로. 화면 표시 변경은 범위 밖 |

## 3. 인터페이스 (추가분)

| 요청 | 응답 |
|---|---|
| `POST /api/writes` + `Idempotency-Key: <키>` (처음) | 201 `{n, environment, hostname, wrote_at}` — 지금과 같음 |
| 같은 키로 다시 | **200** 같은 `n`·`wrote_at`, `environment` 는 그 행의 `written_env` |
| 키 200자 초과 | 400 |
| 스위치 꺼짐 | 헤더 무시, 지금과 같음 |

## 4. 구현 메모

```sql
-- sql/schema.sql 끝에 추가 (IF NOT EXISTS 라 여러 번 실행해도 안전)
ALTER TABLE demo.seq_write ADD COLUMN IF NOT EXISTS request_id text;
CREATE UNIQUE INDEX IF NOT EXISTS seq_write_request_id_key
  ON demo.seq_write (request_id) WHERE request_id IS NOT NULL;
```

```text
insert_next_idempotent(pool, env, key)
  트랜잭션: advisory lock(기존 키 그대로) → SELECT n, wrote_at, written_env WHERE request_id = key
            있으면 (row, created=False)
            없으면 INSERT … SELECT max(n)+1, env, key RETURNING → (row, created=True)
```

- 같은 advisory lock 안에서 조회·삽입하므로 동시에 같은 키가 와도 행은 하나
- 논리 복제: `echo_pub FOR ALL TABLES` 라 `request_id` 값도 B 로 복제된다 → B 에서 같은 키를 알아봄

## 5. 운영 적용 순서 (주의)

논리 복제는 DDL 을 옮기지 않고, 구독 쪽에 없는 칸이 들어오면 B 구독이 멈춘다. 앱은 뜰 때 `AUTO_MIGRATE=true` 로 `schema.sql` 을 돈다 → **도쿄 앱이 새 이미지로 먼저 뜨면 A 에 칸이 먼저 생겨 B 구독이 멈출 수 있다.**

1. 서울 B 에 위 SQL 을 손으로 먼저 실행
2. 도쿄 A 에 실행
3. 새 이미지 배포(스위치 꺼짐)
4. 리허설 때 `IDEMPOTENT_WRITES=true` → 진입점 기록 켬

스위치를 켜기 **전에** 쓴 행에는 `request_id` 가 없다. 그 행의 요청이 재생 구간에 들어 있으면 중복된다 → 진입점 기록은 앱 스위치를 켠 **뒤에** 켠다.

## 6. 완료 기준

- [ ] 기존 시험 전부 그대로 통과(스위치 꺼짐)
- [ ] 같은 키 두 번 → 행 1개, 두 번째 200 + 같은 `n`
- [ ] 같은 키 동시 10번 → 행 1개
- [ ] 키 없음 → 지금과 같음(201, 새 행)
- [ ] 스위치 꺼짐 + 키 있음 → 지금과 같음(키 무시)
- [ ] 키 200자 초과 → 400
- [ ] `schema.sql` 두 번 실행 → 오류 없음
