# 레시피 단계별 지연 로그

레시피 추천 요청의 `requestId`를 `trace_id`로 사용한다. 원격 호출마다 새 `call_id`를 만들며 자동 재시도는 별개의 call_id를 가진다. FastAPI가 내부 HTTP 헤더 `X-Request-ID` / `X-Recipe-Call-ID`로 Worker에 전달한다. 헤더는 로깅에만 사용하며 모델 입력이나 공개 요청 / 응답 JSON에 추가하지 않는다. 오래된 Worker는 헤더를 무시하므로 두 서버 모두 배포해야 전체 연결이 보인다.

## 식별자와 기록 위치

- FastAPI: 기존 비동기 로그 큐 / 날짜별 로그와 표준 출력의 `app.recipes.latency`.
- Worker: Uvicorn 출력의 `uvicorn.error.recipe_latency`. 기본 Uvicorn 로깅에서 INFO도 표시한다. Pod의 Container 로그에서 확인한다.
- `recipe.latency`와 trace_id를 검색한다. jobId는 기존 `recipe.job.queued`의 request_id와 연결한다.
- 원문 / 재고 / 취향 / 인증키 / 헤더 전체를 이 진단 로그에 기록하지 않는다. 비정상 식별자는 지문으로 바꾸며 요청별 ContextVar로 동시 작업을 격리한다.
- backfill처럼 사용자 추천 요청이 아닌 원격 호출도 자체 추적 ID를 생성한다.

## 단계

| phase | 확인할 값 |
| --- | --- |
| fastapi_slot_wait_started / fastapi_slot_acquired | FastAPI AI 슬롯의 wait_ms |
| remote_started / discovery_finished | Pod 자동 탐색 또는 고정 주소 결정의 elapsed_ms |
| remote_error / remote_finished | current_phase(discovery / worker_http), total_ms, phase_ms, outcome, HTTP 상태 |
| worker_started / worker_slot_acquired | Worker 전체 추론 슬롯의 wait_ms |
| llm_slot_acquired | 사진 분석과 공유하는 LLM 슬롯의 wait_ms |
| model_preparation_started / model_preparation_finished | Worker 입력 검증 / 출력 스키마 준비의 elapsed_ms |
| model_started | 입력 byte 수 / context window / 출력 토큰 상한 |
| model_usage | 입력 / 캐시 / 출력 토큰, prompt_ms / generation_ms / load_ms / model_total_ms |
| model_finished | outcome, current_phase(ollama_http_wait / output_validation), total_ms, model_diagnostics_received |
| worker_finished | outcome, 마지막 단계, 해당 단계 경과시간과 전체 경과시간 |
| rank_finished | FastAPI RANK 전체 시간과 시도 횟수 |

모든 시간은 로그 필드명에 표시한 단위이며 `_ms`는 밀리초다. 단계시간은 중첩되므로 모두 합산하지 않는다. remote_finished의 worker_http에는 Worker 슬롯 대기 / 모델 / 네트워크가 포함된다.

## 성공 예시

```text
recipe.latency trace_id=req-123 call_id=- phase=fastapi_slot_acquired details={"wait_ms":0.2}
recipe.latency trace_id=req-123 call_id=call-456 phase=discovery_finished details={"elapsed_ms":780}
recipe.latency trace_id=req-123 call_id=call-456 phase=llm_slot_acquired details={"stage":"RANK","wait_ms":0.1}
recipe.latency trace_id=req-123 call_id=call-456 phase=model_usage details={"prompt_tokens":7400,"cached_prompt_tokens":0,"output_tokens":66,"prompt_ms":1500,"generation_ms":1200,"load_ms":5,"model_total_ms":2900}
```

위 숫자는 설명용이며 실제 측정값이나 운영 로그가 아니다.

## 시간 초과 / 취소 해석

- worker_finished가 llm_slot_wait / timeout이면 모델 슬롯을 기다리다가 제한을 초과했다.
- model_finished가 ollama_http_wait / cancelled이면 Ollama 응답을 받기 전에 취소됐다. Worker timeout의 경우 그 직후 worker_finished에 timeout이 남는다.
- output_validation이면 모델 HTTP 응답 후 JSON / 계약 검증에서 끝났다.
- 모델이 마지막 응답을 주지 못하면 prompt / generation / token 세부값은 알 수 없다. 이를 0으로 만들지 않으며 model_diagnostics_received=false로 표시한다.
- 현재 Ollama stream=false를 유지한다. 첫 토큰 시간이나 취소 시점의 부분 토큰 수를 수집하는 구현은 아니다. 따라서 ollama_http_wait만으로 Ollama 내부 대기와 CUDA 연산을 분리했다고 해석하면 안 된다.
- HTTP 클라이언트 취소가 GPU 추론의 즉시 종료를 보장하지 않는다. 원인 확인 없이 무조건 재시도해 중복 추론을 만들지 않는다.
- 취소와 예외에도 finally 로그를 남기고 세마포어 / 컨텍스트를 복구한다. 프로세스 강제 종료 / SIGKILL에는 finally 실행을 보장하지 않는다.

## 배포와 재시험

FastAPI / Worker 모두 배포 후 dev에서 추천을 한 번 실행한다. 기존 완료 추천 캐시가 적중하면 모델 호출이 없을 수 있으며 캐시 미스를 강제하려고 사용자 재고를 임의 수정하지 않는다. 실제 새 요청의 requestId로 양쪽 로그를 대조한다. 이번 로깅 변경 자체에는 SQL 마이그레이션이 없다. 함께 준비된 맛 특성 기능의 023 SQL은 별도다.

권장 확인 순서: FastAPI 슬롯 → discovery → Worker 슬롯 → LLM 슬롯 → model_usage → output_validation → remote_finished → 기존 추천 완료 / 백엔드 결과 수신 로그.
