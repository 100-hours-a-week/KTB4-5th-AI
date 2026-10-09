# 배포·운영

## 현재 배포 경계

이 저장소는 배포 파일을 제공하지만 새 EC2를 완전히 프로비저닝하지는 않는다. `deploy/deploy.sh`는
Amazon Linux 2023 준비와 원격 `.env`가 끝난 기존 EC2에 코드를 동기화하고 systemd 서비스를
재시작하는 용도다. 최초 배포 전에는 PostgreSQL·DB 사용자·마이그레이션, S3 권한, 운영 인증키,
Worker URL을 별도로 준비해야 한다. 현재 검증된 GPU 전체 경로는 Colab T4이며 고정 RunPod 운영
배포와 RunPod Docker GPU 기동은 아직 검증 전이다.

운영 EC2는 모델을 직접 실행하지 않으므로 `.env`에서 최소한 `APP_ENV=production`,
`INFERENCE_BACKEND=remote`, `IMAGE_SOURCE=s3`와 관련 URL·인증키·버킷을
설정한다. `.env.example`의 `local` 값은 개발용이며 그대로 운영에 사용하지 않는다.

## 로컬 설치

Python 3.11 이상과 Ollama가 필요하다. `uv` 사용을 권장한다.

```bash
cp .env.example .env
uv sync --extra dev --extra ocr
# MobileNetV3 ONNX를 적용할 때만 추가
uv sync --extra vision
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

이 설치가 pip `onnxruntime`도 함께 넣는다. `PADDLE_ORT_DISABLE_ARENA=true`(기본)면 검출·인식 모델을
PaddleX 내장 런타임 대신 이 pip `onnxruntime`으로 메모리 아레나를 끄고 실행한다 — 내장 런타임은
이미지 크기가 바뀔 때마다 메모리가 커져 OOM이 났다. `PADDLE_CPU_THREADS`는 서버 코어 수에 맞춘다.

배포 후 로그에서 교체가 실제로 적용됐는지 확인한다(특히 PaddleX를 업그레이드한 뒤):

```bash
grep "ocr.ort_no_arena" logs/$(date -u +%F).log
```

`installed`와 `session_created`(검출·인식 2건)가 보이면 정상이다. `unavailable`/`session_failed`가
보이면 내장 런타임으로 돌아간 상태이고, 이때는 `PADDLE_ENGINE_RECYCLE_RSS_MB` 안전장치가 메모리를
회수한다. 자세한 메커니즘은 [architecture.md](./architecture.md#동시성과-메모리-관리) 참고.

## 환경변수

전체 기본값과 상세 설명은 [.env.example](../.env.example)이 최신이다. 아래는 그룹별 개요다.

| 그룹 | 변수 | 설명 |
| --- | --- | --- |
| 기본 | `APP_ENV`, `INTERNAL_API_KEY` | 운영/로컬 구분, 서비스 간 인증 키 |
| 로깅 | `LOG_DIR`, `LOG_LEVEL`, `LOG_RETENTION_DAYS` | UTC 날짜별 로그 경로·레벨·보관일수 |
| 큐·동시성 | `ANALYSIS_QUEUE_MAXSIZE`(100), `ANALYSIS_CONSUMER_COUNT`(2) | 대기 큐 용량, 동시 처리 컨슈머 수 — 큐 초과 시 `429 AI_QUEUE_FULL` 반환 |
| 결과 보관 | `ANALYSIS_RESULT_TTL_SECONDS`, `ANALYSIS_RESULT_MAX_ENTRIES` | 인메모리 결과 저장 TTL·최대 개수 |
| 이미지 | `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, `IMAGE_SOURCE`, `LOCAL_IMAGE_ROOT`, `S3_*` | 업로드 제한, 로컬/S3 이미지 소스 |
| 추론 위치 | `INFERENCE_BACKEND`, `REMOTE_INFERENCE_URL`, `REMOTE_INFERENCE_API_KEY`, `REMOTE_IMAGE_BASE_URL` | 로컬 추론 또는 GPU Worker 호출 |
| 입력 유형 | `INPUT_CLASSIFIER_*` | MobileNetV3 ONNX 경로·버전·클래스 순서·신뢰도와 margin 기준·CPU 스레드. 기본 `auto`(체크포인트 있으면 켜고 없으면 자동으로 끔), 강제로 끄려면 `disabled` |
| OCR | `OCR_BACKEND`, `PADDLE_*` | PP-OCRv5 언어·장치·HPI 백엔드·스레드 수·아레나 끄기·엔진 리사이클 임계값 |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_*` | Gemma 연결·타임아웃·`keep_alive`(콜드스타트 방지) |
| DB | `DATABASE_URL`, `DATABASE_POOL_MAX_SIZE`, `REFERENCE_CACHE_MAX_SIZE`, `CATALOG_FUZZY_SIMILARITY_THRESHOLD` | 비우면 카탈로그·소비기한 조회 비활성화. `asyncpg`는 기본 설치됨 |
| 콜백 | `CALLBACK_URL`, `CALLBACK_API_KEY`, `CALLBACK_TIMEOUT_SECONDS`, `CALLBACK_MAX_ATTEMPTS` | 운영에서는 URL·인증 키 필수. 로컬에서만 비우고 GET으로 확인 가능 |

## 원격 GPU Worker

운영에서는 EC2 FastAPI를 `remote` 모드로 두고, RunPod 추론 코드는 저장소의
[`runpod/`](../../runpod/README.md) 폴더 전체를 별도로 배포한다. Worker의
`/internal/v1/infer`는 `X-Worker-API-Key`를 요구하며, 이미지 SHA-256을 확인한 뒤에만 추론한다.

Colab은 외부 상시 서버가 아니라 호환성·속도 측정에만 사용한다. Colab 실험 파일은 배포용 Git에서
제외하므로 운영 배포 절차로 간주하지 않는다.

RunPod에는 저장소 전체를 섞어 올리지 않고 [`runpod/`](../../runpod/README.md) 폴더 전체를 올린다.
현재 검증 경로는 CUDA가 준비된 Pod에서 `setup.sh`와 `start.sh`를 실행하는 방식이다. `Dockerfile`은
GPU CUDA 런타임 기반 이미지로 교체하고 실제 RunPod에서 검증하기 전까지 운영 완료로 간주하지
않는다. Worker에는 PostgreSQL 접속 정보를 넣지 않는다. 카탈로그·소비기한 규칙 조회와 최종 응답
정책은 EC2 API 책임이다.

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

운영에서는 `CALLBACK_URL`을 백엔드의 고정된 `POST /api/v1/internal/image-analyses/callback` 주소로,
`CALLBACK_API_KEY`를 백엔드가 발급한 AI→BE 서비스 인증 키로 반드시 설정한다. 둘 중 하나라도
없으면 `APP_ENV=production` 서버는 시작되지 않는다. 본문 형식은
[architecture.md의 완료 콜백](./architecture.md#완료-콜백)을 따른다. GET은 지속 폴링용이 아니라 완료 제한시간이 지났는데 콜백이 없는 작업의 확인용이다.

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
