# 배포·운영

## 현재 배포 경계

EC2 배포는 `main`에 `fastapi/**` 변경이 머지되면 `ai-cicd.yml`이 이미지를 빌드해 GHCR에 올리고,
KTB4-5th-CLOUD 저장소의 ECS 배포 워크플로가 서비스를 교체한다. 환경변수는 Parameter Store
`/dameokja-v2-dev-ai/*`에서 태스크 정의로 주입된다. PostgreSQL·DB 사용자·마이그레이션, S3 권한, 운영
인증키, Worker URL은 별도로 준비해야 한다. 2026-10-13 기준 dev 환경은 ECS(EC2) FastAPI → RunPod Docker
GPU Worker(RTX A5000) 전체 경로로 동작한다.

EC2 FastAPI는 모델을 실행하지 않고 항상 RunPod GPU Worker를 호출한다. `.env`에서 최소한
`APP_ENV=production`, `REMOTE_INFERENCE_URL`, `REMOTE_INFERENCE_API_KEY`, `IMAGE_SOURCE=s3`와 관련
URL·인증키·버킷을 설정한다. `.env.example`의 `local` 값은 개발용이며 그대로 운영에 사용하지 않는다.

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
| GPU Worker | `REMOTE_INFERENCE_URL`, `REMOTE_INFERENCE_API_KEY`, `REMOTE_INFERENCE_TIMEOUT_SECONDS`, `REMOTE_IMAGE_BASE_URL` | 추론 호출 주소·인증 키·타임아웃 |
| 버전 기록 | `OLLAMA_MODEL`, `INPUT_CLASSIFIER_VERSION`, `OCR_VERSION` 등 | 응답 `modelTrace`에 기록할 Worker 모델 버전 |
| DB | `DATABASE_URL`, `DATABASE_POOL_MAX_SIZE`, `REFERENCE_CACHE_MAX_SIZE`, `CATALOG_FUZZY_SIMILARITY_THRESHOLD` | 비우면 카탈로그·소비기한 조회 비활성화. `asyncpg`는 기본 설치됨 |
| 콜백 | `CALLBACK_URL`, `CALLBACK_API_KEY`, `CALLBACK_TIMEOUT_SECONDS`, `CALLBACK_MAX_ATTEMPTS` | 운영에서는 URL·인증 키 필수. 로컬에서만 비우고 GET으로 확인 가능 |
| 콜백 outbox | `CALLBACK_OUTBOX_ENABLED`(false), `CALLBACK_OUTBOX_MAX_RETRY_DELAY_SECONDS`(60), `CALLBACK_OUTBOX_EXPIRES_SECONDS`(86400) 등 | 켜면 완료 콜백을 DB에 저장하고 재시작 후에도 다시 보낸다. `analysis_callback_outbox` 테이블을 먼저 만들고 `DATABASE_URL`과 함께 켠다 |

## 원격 GPU Worker

RunPod 추론 코드는 저장소의 [`runpod/`](../../runpod/README.md) 폴더 전체를 별도로 배포한다. Worker의
`/internal/v1/infer`는 `X-Worker-API-Key`를 요구하며, 이미지 SHA-256을 확인한 뒤에만 추론한다.

Colab은 외부 상시 서버가 아니라 호환성·속도 측정에만 사용한다. Colab 실험 파일은 배포용 Git에서
제외하므로 운영 배포 절차로 간주하지 않는다.

RunPod에는 저장소 전체를 섞어 올리지 않고 [`runpod/`](../../runpod/README.md) 폴더 전체를 올린다.
`main`에 `runpod/**` 변경이 머지되면 `runpod-cicd.yml`이 이미지를 빌드해 GHCR에 올리고
`RUNPOD_POD_ID` Pod의 이미지를 교체한다. Worker에는 PostgreSQL 접속 정보를 넣지 않는다. 카탈로그·소비기한 규칙 조회와 최종 응답
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
    "image": {
      "objectKey": "receipt.png",
      "sha256": "64자리_SHA256"
    },
    "inputHint": "RECEIPT",
    "locale": "ko-KR",
    "timezone": "Asia/Seoul"
  }'
```

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
있다. API 키, SHA-256, OCR 근거 원문과 상세 식재료 결과는 로그에 기록하지 않거나
마스킹한다. 호출부는 `QueueHandler`로 로그를 메모리 큐에 넣고 즉시 반환하며, 별도
`QueueListener` 스레드가 같은 형식으로 파일에 기록한다. 로그 시각과 파일 날짜는 UTC를
사용하며, UTC 00:00 이후 첫 로그부터 새 `yyyy-mm-dd.log`에 기록한다. 날짜 로그는 기본
14일 보관하고 서버 종료 시 남은 큐를 모두 기록한 뒤 파일을 닫는다.
