# 이미지 분석 Kafka 연동

`KAFKA_ENABLED=true`로 활성화한다. 기본값은 `false`이며 기존 HTTP API도 유지한다.
Kafka 요청은 기존 분석 런타임을 실행하고 `build_callback_payload`로 만든 동일한 result/error를
완료 이벤트에 넣는다. Kafka로만 접수한 분석은 HTTP 콜백을 호출하지 않는다.
완료 전달 전 같은 요청을 HTTP와 Kafka로 모두 접수하면 분석은 한 번 실행하고 Kafka 완료 이벤트를 우선한다. HTTP 콜백은 중복 전송하지 않는다.
OCR·모델 호출·분류·레시피·학습 정책은 기존 실행 경로를 그대로 사용한다.

## 설정

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `KAFKA_ENABLED` | `false` | Kafka 어댑터 활성화 |
| `KAFKA_ENV` | `local` | 토픽과 AI 소비 그룹의 환경 접두사 |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | 브로커 목록(쉼표 구분 가능), PLAINTEXT |
| `KAFKA_MAX_POLL_INTERVAL_MS` | `300000` | 소비 polling 간격 제한 |
| `KAFKA_RETRY_DELAY_SECONDS` | `5` | 처리·발행·커밋 실패 후 같은 레코드 재시도 간격 |
| `KAFKA_SHUTDOWN_TIMEOUT_SECONDS` | `10` | 종료 시 producer flush 최대 대기 시간 |

production에서는 기존 HTTP 작업 완료를 전달할 CALLBACK_URL과 CALLBACK_API_KEY도 설정한다. 피드백 저장에는 기존 `DATABASE_URL`과 마이그레이션이 필요하다.

토픽은 서버 운영자가 미리 준비해야 한다. 앱은 토픽 생성 API를 호출하지 않으며, 구독 전에
전체 토픽 목록으로 요청·피드백·완료 토픽의 존재를 확인한다. 누락되면 시작에 실패한다.

| 방향 | 토픽 | 소비 그룹 |
| --- | --- | --- |
| BE → AI | `${KAFKA_ENV}.dameokja.analysis.requested.v1` | `${KAFKA_ENV}.dameokja.ai.analysis-requested.v1` |
| AI → BE | `${KAFKA_ENV}.dameokja.analysis.completed.v1` | BE가 관리 |
| BE → AI | `${KAFKA_ENV}.dameokja.analysis.feedback.v1` | `${KAFKA_ENV}.dameokja.ai.analysis-feedback.v1` |

BE 결과 소비용 `.completed.v1.dlt`는 사용하지 않는다. 별도 피드백 응답 이벤트도 발행하지 않는다.

## 이벤트와 소비 완료

모든 이벤트는 `{"messageId":"UUID","payload":{...}}`이며 Kafka key는 UTF-8 `userId` 문자열,
`payload.userId`는 JSON 정수다. 이벤트 종류는 토픽 이름으로 구분하며 `eventType`은 넣지 않는다.
분석 요청의 `payload.refrigeratorId`는 필수 양의 JSON 정수다. 입력 경계에서 UUID·시간대 포함 시각·key 일치를
확인한 뒤 기존 `AnalysisCreateRequest`/`FeedbackRequest` 검증을 수행한다.
분석 완료까지 요청의 `requestId`, `userId`, `refrigeratorId`를 보존한다.

- 요청: 접수 반환이나 내부 `queue.task_done()`은 커밋 조건이 아니다. terminal 결과를 기다려
  `COMPLETED` 또는 `FAILED` 이벤트를 발행하고 브로커 ACK를 받은 뒤 해당 파티션의 `offset+1`만
  명시적으로 커밋한다. producer는 `acks=all`, `enable_idempotence=true`를 사용한다.
- 완료: 기존 콜백의 `analysisId/status/completedAt/result/error`에
  `occurredAt/requestId/userId/refrigeratorId`를 붙인다. UTC 시각은 `Z` 형식이며 result/error의 null을
  유지한다. 저장된 결과와 같은 입력 메시지로 재전송하면 messageId와 본문이 같다.
- 피드백: 기존 `FeedbackService.submit(analysisId, request)`가 저장·학습 반영을 마친 뒤 커밋한다.
  네 action과 `expirationSource`, 수량·중량·null을 전달하고 기존 analysisId 중복 처리를 사용한다.
- 파티션: 한 건을 처리하는 동안 해당 파티션을 pause한다. 다른 파티션과 polling은 계속 진행한다.
  같은 파티션의 뒤 레코드가 앞선 미완료 offset을 넘어 커밋되지 않는다.
- 실패: 처리·발행·커밋 예외 시 같은 레코드를 재시도하고 뒤 레코드는 읽지 않는다. 잘못된 계약이나
  복구되지 않는 장애도 로그를 남기며 해당 파티션을 막는다. 자동 건너뛰기나 DLT 전송은 하지 않는다.
- 재할당·종료: 처리 대기를 취소하며 미완료 offset은 커밋하지 않는다. 대기 취소는 이미 내부 큐에
  접수된 분석을 취소하지 않는다. producer flush가 종료 제한을 넘으면 오류를 기록하고 앱 자원 정리를
  진행한다. ACK 여부가 불명확한 발행은 이미 브로커에 도착했을 수도 있으므로 중복 전달이 가능하다.

## 멱등성의 보장 범위

이는 exactly-once 처리가 아니다. 현재 저장소가 같은 프로세스 메모리에 결과를 보관하는 동안에는
기존 requestId 멱등성으로 재분석 없이 같은 analysisId·결과를 다시 발행한다. 같은 requestId에 다른
입력은 기존 충돌 검증으로 거부한다. 피드백은 기존 DB의 analysisId 중복 처리에 따른다.

프로세스 재시작, 결과 TTL 만료, 저장소 용량에 의한 제거 뒤에는 requestId 색인이 사라진다.
ACK 후 offset 커밋 전 장애에서 재전달되면 새 analysisId와 결과가 생겨 BE가 충돌로 거부할 수 있다.
선택 PostgreSQL 이력은 분석 ID 조회·피드백용이며, requestId 복구에 쓰이지 않는다. 기존 이력 저장의
실패도 계속 분석 성공과 별개로 취급한다. 여러 프로세스 사이의 분석 멱등성도 보장하지 않는다.
이 범위까지 보장하려면 사용자와 별도로 영속 요청·결과 복구 방식을 정해야 한다.

## 로컬 검증

`uv sync --extra dev --frozen`으로 환경을 준비한 뒤 로컬
`uv run --frozen pytest -q tests/test_analysis_kafka.py`로 어댑터 테스트를 실행한다.
저장소의 `.gitignore` 정책상 `tests/`는 로컬에만 보관되므로 새 clone에는 이 검증 파일이 없다.
테스트는 제어 가능한 future/event를 사용하며 실브로커·GPU·클라우드·DB를 호출하지 않는다.

클라이언트 동작은 aiokafka 공식 문서의
[manual commit·rebalance](https://aiokafka.readthedocs.io/en/stable/consumer.html),
[producer ACK·idempotence](https://aiokafka.readthedocs.io/en/stable/producer.html),
[poll 간격과 heartbeat](https://aiokafka.readthedocs.io/en/stable/api.html#aiokafka.AIOKafkaConsumer)를
확인했다(2026-11-01). heartbeat만으로 장기 작업을 맡기지 않고 별도 polling을 유지한다.
