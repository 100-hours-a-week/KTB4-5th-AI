# 배포·운영

## 로컬 설치

Python 3.11 이상과 Ollama가 필요하다. `uv` 사용을 권장한다.

```bash
cp .env.example .env
uv sync --extra dev --extra ocr
ollama pull gemma4:e2b
mkdir -p var/uploads
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

또는 실행 스크립트를 쓴다. 스크립트는 `.venv`, `.env`가 준비되어 있다고 가정하며 PID는
`var/run`, 애플리케이션 로그는 UTC 날짜별 `logs/yyyy-mm-dd.log`, 표준 출력은
`logs/server.out.log`에 저장한다.

```bash
chmod +x start.sh stop.sh restart.sh
./start.sh
./restart.sh
./stop.sh
```

서버 동작 확인은 `GET /health/live`, `GET /health/ready`, `GET /ai/v1/models/status`와
아래 [로컬 이미지로 요청하기](#로컬-이미지로-요청하기)의 `curl` 예시로 한다.

**`--workers 1`은 반드시 유지해야 한다.** worker를 여러 개 띄우면 프로세스마다 별도의
큐·인메모리 store를 갖게 되어, 같은 `analysisId`를 조회해도 어느 worker가 응답하느냐에
따라 결과가 달라질 수 있다. 처리량은 `ANALYSIS_CONSUMER_COUNT`(같은 프로세스 안의 동시
처리 개수)로 조절한다 — [architecture.md의 동시성과 메모리 관리](./architecture.md#동시성과-메모리-관리)
참고.

PaddleOCR/PaddlePaddle 설치가 지원되지 않는 아키텍처(예: 일부 macOS)에서는 Linux x86_64
또는 EC2/Docker에서 실행해야 한다. 테스트는 실제 모델을 다운로드하지 않고 가짜 어댑터로
실행된다.

```bash
uv run ruff check .
uv run pytest -q
```

## PP-OCR 고성능 추론(HPI) 설정

`.env`의 `PADDLE_HPI_BACKEND=onnxruntime`(text_detection/text_recognition만 ONNX Runtime
으로 강제)을 쓰려면 서버에서 한 번 의존성을 설치해야 한다(venv에 `pip` 모듈이 없으면 먼저
`uv pip install --python .venv/bin/python pip`부터 실행).

```bash
source .venv/bin/activate  # paddlex CLI가 PATH에 있어야 설치가 됨
paddleocr install_hpi_deps cpu
```

이 백엔드는 이미지 크기가 바뀔 때마다 메모리 아레나가 커지고 줄지 않는 특성이 있어(실측:
OOM으로 프로세스가 죽은 적 있음), `PADDLE_ENGINE_RECYCLE_AFTER`/`PADDLE_ENGINE_RECYCLE_RSS_MB`
로 주기적으로 엔진을 리사이클한다. 자세한 메커니즘은
[architecture.md](./architecture.md#동시성과-메모리-관리) 참고.

## 환경변수

전체 기본값과 상세 설명은 [.env.example](../.env.example)이 최신이다. 아래는 그룹별 개요다.

| 그룹 | 변수 | 설명 |
| --- | --- | --- |
| 기본 | `APP_ENV`, `INTERNAL_API_KEY` | 운영/로컬 구분, 서비스 간 인증 키 |
| 로깅 | `LOG_DIR`, `LOG_LEVEL`, `LOG_RETENTION_DAYS` | UTC 날짜별 로그 경로·레벨·보관일수 |
| 큐·동시성 | `ANALYSIS_QUEUE_MAXSIZE`(40), `ANALYSIS_CONSUMER_COUNT`(2) | 큐 용량, 동시 처리 컨슈머 수 — 실측 근거는 architecture.md |
| 결과 보관 | `ANALYSIS_RESULT_TTL_SECONDS`, `ANALYSIS_RESULT_MAX_ENTRIES` | 인메모리 결과 저장 TTL·최대 개수 |
| 이미지 | `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, `IMAGE_SOURCE`, `LOCAL_IMAGE_ROOT`, `S3_*` | 업로드 제한, 로컬/S3 이미지 소스 |
| OCR | `OCR_BACKEND`, `PADDLE_*` | PP-OCRv5 언어·장치·HPI 백엔드·엔진 리사이클 임계값 |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_*` | Gemma 연결·타임아웃·`keep_alive`(콜드스타트 방지) |
| DB | `DATABASE_URL`, `DATABASE_POOL_MAX_SIZE`, `REFERENCE_CACHE_MAX_SIZE`, `CATALOG_FUZZY_SIMILARITY_THRESHOLD` | 비우면 카탈로그·소비기한 조회 비활성화 |
| 콜백 | `CALLBACK_URL`, `CALLBACK_API_KEY`, `CALLBACK_TIMEOUT_SECONDS`, `CALLBACK_MAX_ATTEMPTS` | 비우면 콜백 없이 GET 폴링만 |

## 로컬 이미지로 요청하기

이미지를 `var/uploads/receipt.png`에 둔 뒤 해시를 계산한다.

```bash
shasum -a 256 var/uploads/receipt.png
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

운영에서는 `CALLBACK_URL`을 백엔드의 고정된 `POST /internal/v1/ai-analysis-results` 주소로
설정한다. GET은 지속 폴링용이 아니라 완료 제한시간이 지났는데 콜백이 없는 작업의 확인용이다.

## Docker

이미지는 PP-OCR CPU 의존성을 포함하므로 빌드 시간이 걸릴 수 있다.

```bash
docker build -t ktb-receipt-ai .
docker run --rm -p 8000:8000 --env-file .env ktb-receipt-ai
```

Ollama를 호스트에서 실행하고 FastAPI만 컨테이너로 실행한다면 `OLLAMA_BASE_URL`을
컨테이너에서 접근 가능한 호스트 주소로 설정해야 한다.

## 로깅

모든 HTTP 요청에는 `X-Request-ID`가 있으면 해당 값을, 없으면 서버가 만든 `traceId`를
사용하여 요청·응답 로그 두 줄을 남긴다. 응답 헤더 `X-Trace-ID`에서도 같은 값을 확인할 수
있다. API 키, SHA-256, OCR 근거 원문과 상세 식재료 결과는 로그에 기록하지 않거나
마스킹한다. 호출부는 `QueueHandler`로 로그를 메모리 큐에 넣고 즉시 반환하며, 별도
`QueueListener` 스레드가 같은 형식으로 파일에 기록한다. 로그 시각과 파일 날짜는 UTC를
사용하며, UTC 00:00 이후 첫 로그부터 새 `yyyy-mm-dd.log`에 기록한다. 날짜 로그는 기본
14일 보관하고 서버 종료 시 남은 큐를 모두 기록한 뒤 파일을 닫는다.
