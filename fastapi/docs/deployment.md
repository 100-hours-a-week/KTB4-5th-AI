# 배포·운영

## 현재 배포 경계

EC2 배포는 `main`에 `fastapi/**` 변경이 머지되면 `ai-cicd.yml`이 이미지를 빌드해 GHCR에 올리고,
KTB4-5th-CLOUD 저장소의 ECS 배포 워크플로가 서비스를 교체한다. 환경변수는 Parameter Store
`/dameokja-v2-dev-ai/*`에서 태스크 정의로 주입된다. PostgreSQL·DB 사용자·마이그레이션, S3 권한, 운영
인증키, Worker URL은 별도로 준비해야 한다. 2026-10-13 기준 dev 환경은 ECS(EC2) FastAPI → RunPod Docker
GPU Worker(RTX A5000) 전체 경로로 동작한다.

EC2 FastAPI는 모델을 실행하지 않고 항상 RunPod GPU Worker를 호출한다. `.env`에서 최소한
`APP_ENV=production`, `REMOTE_INFERENCE_API_KEY`, `IMAGE_SOURCE=s3`와 관련 인증키·버킷을 설정한다.
Worker 주소는 고정 `REMOTE_INFERENCE_URL` 또는 아래 Runpod 자동 탐색 설정 중 하나를 사용한다.
`.env.example`의 `local` 값은 개발용이며 그대로 운영에 사용하지 않는다.

```env
RUNPOD_API_KEY=<pods:read 권한 키>
RUNPOD_POD_NAME_PREFIX=dameokja-ai-worker
RUNPOD_TEMPLATE_ID=5up36sps5z
RUNPOD_WORKER_PORT=9000
RUNPOD_DISCOVERY_TTL_SECONDS=60
```

자동 탐색은 `RUNNING` 상태인 `<prefix>-vN` Pod를 버전 내림차순으로 검사하고 `/health/ready`가
준비된 첫 Worker를 사용한다. 주소는 기본 60초 동안 캐시하며 추론 요청의 연결 오류 또는 5xx 응답에는
캐시를 비우고 한 번 다시 탐색한다. dev·stage가 Worker를 공유한다면 같은 prefix를 사용하고, prod를
분리할 때는 `dameokja-ai-worker-nonprod`, `dameokja-ai-worker-prod`처럼 환경마다 prefix를 다르게 둔다.

## 로컬 설치

Python 3.11 이상이 필요하고 `uv` 사용을 권장한다. 분석 요청을 실제로 처리하려면 접근 가능한
GPU Worker(`REMOTE_INFERENCE_URL`)가 있어야 한다. Worker 없이도 서버는 뜨지만 `/health/ready`가
준비되지 않음으로 응답하고 분석은 실패로 끝난다.

```bash
cp .env.example .env
uv sync --extra dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

서버 동작 확인은 `GET /health/live`, `GET /health/ready`, `GET /ai/v1/models/status`와
아래 [로컬 이미지로 요청하기](#로컬-이미지로-요청하기)의 `curl` 예시로 한다.

**`--workers 1`은 반드시 유지해야 한다.** worker를 여러 개 띄우면 프로세스마다 별도의
큐·인메모리 store를 갖게 되어, 같은 `analysisId`를 조회해도 어느 worker가 응답하느냐에
따라 결과가 달라질 수 있다. 처리량은 `ANALYSIS_CONSUMER_COUNT`(같은 프로세스 안의 동시
처리 개수)로 조절한다 — [architecture.md의 동시성과 메모리 관리](./architecture.md#동시성과-메모리-관리)
참고.

테스트는 Worker를 호출하지 않고 Worker 응답을 흉내 낸 가짜 어댑터로 실행된다.

```bash
uv run ruff check .
uv run pytest -q
```

## 환경변수

전체 기본값과 상세 설명은 [.env.example](../.env.example)이 최신이다. 아래는 그룹별 개요다.

| 그룹 | 변수 | 설명 |
| --- | --- | --- |
| 기본 | `APP_ENV`, `INTERNAL_API_KEY` | 운영/로컬 구분, 서비스 간 인증 키 |
| 로깅 | `LOG_DIR`, `LOG_LEVEL`, `LOG_RETENTION_DAYS` | UTC 날짜별 로그 경로·레벨·보관일수 |
| 큐·동시성 | `ANALYSIS_QUEUE_MAXSIZE`(100), `ANALYSIS_CONSUMER_COUNT`(2) | 대기 큐 용량, 동시 처리 컨슈머 수 — 큐 초과 시 `429 AI_QUEUE_FULL` 반환 |
| 결과 보관 | `ANALYSIS_RESULT_TTL_SECONDS`, `ANALYSIS_RESULT_MAX_ENTRIES` | 인메모리 결과 저장 TTL·최대 개수 |
| 이미지 | `IMAGE_SOURCE`, `S3_*` | Worker에 넘길 이미지 위치(S3 서명 URL 또는 개발용 기준 URL). 크기·픽셀 제한은 Worker가 검증 |
| GPU Worker | `REMOTE_INFERENCE_URL` 또는 `RUNPOD_API_KEY`·`RUNPOD_POD_NAME_PREFIX`, `RUNPOD_TEMPLATE_ID`, `RUNPOD_WORKER_PORT`, `REMOTE_INFERENCE_API_KEY`, `REMOTE_INFERENCE_TIMEOUT_SECONDS`, `REMOTE_IMAGE_BASE_URL` | 고정 주소 또는 실행 중인 Worker 자동 탐색, 호출 인증 키·타임아웃 |
| 버전 기록 | `OLLAMA_MODEL`, `INPUT_CLASSIFIER_VERSION`, `OCR_VERSION` 등 | 응답 `modelTrace`에 기록할 Worker 모델 버전 |
| DB | `DATABASE_URL`, `DATABASE_POOL_MAX_SIZE`, `REFERENCE_CACHE_MAX_SIZE`, `PRODUCT_CATALOG_CACHE_TTL_SECONDS`, `CATALOG_FUZZY_SIMILARITY_THRESHOLD` | 비우면 카탈로그·소비기한 조회 비활성화. 상품 카탈로그 캐시는 TTL 안에 다른 프로세스의 학습 승격을 다시 읽으며, `asyncpg`는 기본 설치됨 |
| 피드백 학습 | `FEEDBACK_PROMOTION_MIN_SUPPORT`(3), `FEEDBACK_PROMOTION_MIN_AGREEMENT`(0.8) | 학습 지식 승격 기준. `DATABASE_URL`이 없으면 피드백 API는 503 |
| 콜백 | `CALLBACK_URL`, `CALLBACK_API_KEY`, `CALLBACK_TIMEOUT_SECONDS`, `CALLBACK_MAX_ATTEMPTS` | 운영에서는 URL·인증 키 필수. 로컬에서만 비우고 GET으로 확인 가능 |

## 원격 GPU Worker

RunPod 추론 코드는 저장소의 [`runpod/`](../../runpod/README.md) 폴더 전체를 별도로 배포한다. Worker의
`/internal/v1/infer`는 `X-Worker-API-Key`를 요구하며, 이미지 SHA-256을 확인한 뒤에만 추론한다.

Colab은 외부 상시 서버가 아니라 호환성·속도 측정에만 사용한다. Colab 실험 파일은 배포용 Git에서
제외하므로 운영 배포 절차로 간주하지 않는다.

RunPod에는 저장소 전체를 섞어 올리지 않고 [`runpod/`](../../runpod/README.md) 폴더 전체를 올린다.
`main`에 `runpod/**` 변경이 머지되면 `runpod-cicd.yml`이 이미지를 빌드해 GHCR에 올리고
실행 중인 `<RUNPOD_POD_NAME_PREFIX>-vN` 가운데 가장 높은 버전의 Pod를 찾아 이미지를 교체한다.
GitHub 저장소에는 `RUNPOD_API_KEY` Secret과 선택적인 `RUNPOD_POD_NAME_PREFIX`, `RUNPOD_TEMPLATE_ID`
Variable만 설정하며 고정 Pod ID는 저장하지 않는다. Worker에는 PostgreSQL 접속 정보를 넣지 않는다. 카탈로그·소비기한 규칙 조회와 최종 응답
정책은 EC2 API 책임이다.

## 로컬 이미지로 요청하기

`IMAGE_SOURCE=local`이면 Worker가 `REMOTE_IMAGE_BASE_URL` 아래에서 이미지를 내려받는다. 그 주소로
제공할 이미지를 `receipt.png`로 두고 해시를 계산한다.

```bash
shasum -a 256 receipt.png
```

```bash
curl -X POST http://127.0.0.1:8000/ai/v1/analyses \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-API-Key: change-me' \
  -d '{
    "requestId": "req_receipt_001",
    "refrigeratorId": 101,
    "image": {
      "objectKey": "receipt.png",
      "sha256": "64자리_SHA256"
    },
    "inputHint": "RECEIPT",
    "locale": "ko-KR",
    "timezone": "Asia/Seoul"
  }'
```

`refrigeratorId`는 생략할 수 있지만, 없으면 개인 보정 지식을 조회할 수 없다. 사용자가 이전에
고친 상품명·카테고리를 같은 냉장고의 다음 분석부터 즉시 적용하려면 백엔드가 반드시 전달한다.

응답의 `analysisId`는 콜백이 없는 로컬 환경에서만 GET으로 확인한다.

```bash
curl http://127.0.0.1:8000/ai/v1/analyses/ana_xxx \
  -H 'X-Internal-API-Key: change-me'
```

운영에서는 `CALLBACK_URL`을 백엔드의 고정된 `POST /api/v1/internal/image-analyses/callback` 주소로,
`CALLBACK_API_KEY`를 백엔드가 발급한 AI→BE 서비스 인증 키로 반드시 설정한다. 둘 중 하나라도
없으면 `APP_ENV=production` 서버는 시작되지 않는다. 본문 형식은
[architecture.md의 완료 콜백](./architecture.md#완료-콜백)을 따른다. GET은 지속 폴링용이 아니라 완료 제한시간이 지났는데 콜백이 없는 작업의 확인용이다.

## Docker

이미지에는 모델·OCR 의존성이 없다.

```bash
docker build -t ktb-receipt-ai .
docker run --rm -p 8000:8000 --env-file .env ktb-receipt-ai
```

## 로깅

모든 HTTP 요청에는 `X-Request-ID`가 있으면 해당 값을, 없으면 서버가 만든 `traceId`를
사용하여 요청·응답 로그 두 줄을 남긴다. 응답 헤더 `X-Trace-ID`에서도 같은 값을 확인할 수
있다. 호출부는 `QueueHandler`로 로그를 메모리 큐에 넣고 즉시 반환하며, 별도
`QueueListener` 스레드가 같은 형식으로 파일에 기록한다. 로그 시각과 파일 날짜는 UTC를
사용하며, UTC 00:00 이후 첫 로그부터 새 `yyyy-mm-dd.log`에 기록한다. 날짜 로그는 기본
14일 보관하고 서버 종료 시 남은 큐를 모두 기록한 뒤 파일을 닫는다.

### 로그 기록 정책

| 구분 | 기록하는 내용 |
| --- | --- |
| 요청 감사 로그 | 경로·HTTP 상태·처리 시간과 요청 파라미터(`objectKey`, `inputHint` 등). `sha256`·`evidenceText`와 이름에 `apiKey`·`api_key`·`token`·`secret`·`password`가 들어간 필드는 `[REDACTED]`로 바꾼다. 분석 조회 응답은 상태와 상품 수만 남긴다. `/metrics` 수집 요청은 기록하지 않는다 |
| 콜백 추적 로그 | 콜백 준비(`callback.payload.ready`), 성공, 거부, 재시도 소진 시점에 백엔드로 보낸 본문 요약을 남긴다. `analysisId`·`status`·`documentType`·상품 수와 상품별 `itemId`·`displayStatus`·상품 후보명·카테고리·보관 방식·측정 방식·수량·중량·소비기한·`reviewReasons`가 포함된다. 백엔드가 거부하면 응답 본문을 최대 2,000자까지 남긴다 |
| 기록하지 않는 내용 | 원본 이미지와 S3 서명 URL, API 키와 인증 헤더, SHA-256, OCR 원문 전체와 `evidenceText`. 카드번호·전화번호는 OCR 단계에서 이미 `[REDACTED]`로 마스킹된다 |

상품 후보명과 수량은 개인을 식별하는 정보가 아니고, 백엔드가 콜백을 거부하거나 받지 못했을 때 AI가
실제로 무엇을 보냈는지 재현해야 하므로 기록한다. 이 값은 사용자가 확정한 재고가 아니라 AI가 만든
후보값이다. 날짜별 파일 로그는 `LOG_RETENTION_DAYS`(기본 14일)만 보관하며, 같은 내용이 표준 출력으로
ECS를 거쳐 CloudWatch Logs에도 쌓이므로 로그 그룹 보관기간도 14일 이하로 맞춘다. 로그 열람 권한은
운영 담당자로 제한한다.

## 모니터링

FastAPI는 내부 인증이 적용된 `GET /metrics`에서 Prometheus 형식의 지표를 제공한다. 수집기는
분석 API와 동일하게 `X-Internal-API-Key`를 보내야 한다. 기본 Python 프로세스 CPU·메모리 지표와
함께 다음 서비스 지표를 확인할 수 있다.

- 접수 결과(`accepted`, `idempotent`, `queue_full`), 현재 큐 크기와 가장 오래된 대기 시간
- 현재 처리 중인 분석 수, 큐 대기 시간, 실제 분석 시간, 완료·실패 수
- 결과 저장소 크기, 결과당 상품 수, `RECOGNIZED`·`AI_ESTIMATED`·`NEEDS_REVIEW` 상품 수
- 콜백 성공·재시도·거부·실패 수와 콜백 HTTP 지연 시간

GPU 단계 지표는 Worker의 `GET /metrics`에서 별도로 수집한다. 두 endpoint 모두 분석 ID, 요청 ID,
상품명과 OCR 원문을 metric label로 사용하지 않는다. Prometheus나 Grafana 수집 장애도 분석 흐름에는
영향을 주지 않는다. 운영 대시보드에서는 FastAPI의 큐 지표와 Worker의 단계 지표를 같은 시간축으로
비교해야 병목 위치를 구분할 수 있다.

FastAPI는 정상 수신한 Worker 응답의 `diagnostics`도 자체 `/metrics`에 다시 집계한다. 따라서 AWS
수집기는 RunPod endpoint를 직접 호출하지 않고도 `dameokja_remote_worker_stage_duration_seconds`,
`dameokja_remote_llm_tokens_total`, `dameokja_remote_llm_throughput_tokens_per_second`,
`dameokja_remote_llm_model_load_duration_seconds`에서 GPU 단계 시간과 LLM 토큰 사용량을 확인할 수
있다. 이 계측은 이미 응답에 포함된 숫자를 메모리 지표에 반영할 뿐이며 추가 추론·이미지 다운로드·DB
저장을 수행하지 않는다. 재시도로 Worker 추론이 다시 수행되면 실제로 소비된 자원을 반영하기 위해 각
성공 응답의 진단값을 모두 집계한다.

Prometheus 수집 인프라가 준비되기 전의 1차 경보는 CloudWatch Logs 지표 필터로 건다.
[`monitoring/cloudwatch-log-alarms.sh`](../monitoring/cloudwatch-log-alarms.sh)가 FastAPI 로그 그룹에서 콜백
재시도 소진·거부, GPU Worker 호출 실패, 분석 실패, 큐 포화, DB 조회 실패 로그 건수를 지표로 만들고 SNS
경보를 연결한다. 이 경보는 로그 메시지의 이벤트 이름(`callback.exhausted` 등)에 의존하므로 로그 문구를
바꾸면 스크립트의 패턴도 함께 고친다. 요청이 없으면 실패 로그도 없으므로, 트래픽이 없는 시간의 Worker
정지는 별도의 `/health/ready` 주기 점검으로 확인해야 한다.

Prometheus용 초기 경보 규칙은 [`monitoring/prometheus-alerts.yml`](../monitoring/prometheus-alerts.yml)에 있다.
실제 부하와 사용자 허용 지연을 확인한 뒤 임계값을 조정한다. 주기적인 `/metrics` 수집 요청은 날짜별
API 감사 로그에서 제외해 실제 분석 요청 로그가 묻히지 않도록 한다.
