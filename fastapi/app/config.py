from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ktb-receipt-ai"
    app_env: Literal["local", "test", "production"] = "local"
    internal_api_key: str | None = None
    log_dir: str = "./logs"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_retention_days: int = Field(default=14, ge=1, le=365)

    # 큐 항목 자체는 이미지 바이트 없이 objectKey·sha256만 들고 있는 가벼운 레코드라, 크기를
    # 늘려도 메모리 비용은 작다. 대기 작업은 최대 100개까지만 받아 순간 유입을 흡수하고,
    # 그 이상은 AI_QUEUE_FULL로 즉시 거절한다. 처리량 자체는 analysis_consumer_count가 결정한다.
    analysis_queue_maxsize: int = Field(default=100, ge=1, le=10_000)
    # 분석 파이프라인을 동시에 몇 개까지 처리할지. OCR 엔진은 PaddleTextExtractor 안의
    # asyncio.Lock으로 여전히 1개씩만 돈다(메모리 아레나 문제 때문에 의도적으로 유지) —
    # 늘어나는 건 그 앞뒤(이미지 로드·Gemma 분류·DB 조회)가 겹칠 수 있게 되는 부분이다.
    # 실측: 큐 대기 127초 중 실제 처리는 13.6초(OCR 9초+Gemma 4.6초)뿐이라 대부분이 대기시간
    # 이었다 — 컨슈머를 늘리면 이 대기시간이 줄어든다.
    #
    # 다만 EC2가 4코어뿐이고 Ollama(gemma4:e2b)도 GPU 없이 CPU 추론이라, 컨슈머를 3개로 돌려
    # 20개 동시 부하를 실측했더니 건당 처리시간이 평균 32.8초·최대 79.7초까지 늘어났다(원래
    # 13초대). 2개로 낮추니 큐 대기 개선은 비슷하면서 처리시간은 평균 26.8초·최대 54.2초로
    # 덜 느려져서 2를 기본값으로 뒀다 — 코어 수보다 늘리면 서로 CPU를 뺏어 전체가 느려진다.
    analysis_consumer_count: int = Field(default=2, ge=1, le=16)
    analysis_result_ttl_seconds: int = Field(default=3600, ge=60)
    analysis_result_max_entries: int = Field(default=1000, ge=1)
    analysis_retry_count: int = Field(default=1, ge=0, le=3)
    analysis_retry_delay_seconds: float = Field(default=1.0, ge=0, le=30)
    max_image_bytes: int = Field(default=15 * 1024 * 1024, ge=1024)
    max_image_pixels: int = Field(default=40_000_000, ge=1_000_000)

    input_classifier_backend: Literal["auto", "disabled", "onnx"] = "auto"
    input_classifier_model_path: str = "./models/input-type-mobilenetv3-small.onnx"
    input_classifier_labels: str = "OTHER,PRODUCT,RECEIPT"
    input_classifier_min_confidence: float = Field(default=0.8, ge=0, le=1)
    input_classifier_min_margin: float = Field(default=0.2, ge=0, le=1)
    input_classifier_cpu_threads: int = Field(default=1, ge=1, le=64)

    inference_backend: Literal["local", "remote"] = "local"
    remote_inference_url: str | None = None
    remote_inference_api_key: str | None = None
    remote_inference_timeout_seconds: float = Field(default=180.0, gt=0)
    remote_image_base_url: str | None = None

    image_source: Literal["s3", "local"] = "local"
    local_image_root: str = "./var/uploads"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_allowed_prefix: str = "uploads/"

    ocr_backend: Literal["paddle", "stub"] = "paddle"
    paddle_lang: str = "korean"
    paddle_ocr_version: str = "PP-OCRv5"
    paddle_device: str = "cpu"
    paddle_enable_mkldnn: bool = False
    # 문서 방향 분류(PP-LCNet_x1_0_doc_ori)는 손에 들고 비스듬히 찍은 정상 방향 영수증을 뒤집힌
    # 사진으로 잘못 판단해 180° 돌려버리는 경우가 있었다 — 롯데마트 영수증이 "OEL 6" 같은 거꾸로 읽힌
    # 글자만 나와 OTHER(점수 0.15)가 됐고, 끄자 0.87로 정상 인식됐다. 과거 배치 109장 비교(2026-09-22):
    # 영수증 91장은 켬/끔 모두 91장 인식(나빠진 것 0), OTHER로 잘못 떨어졌던 18장 중 실제 영수증
    # 4장이 끔에서만 살아났다. 스마트폰 사진은 EXIF로 방향이 이미 맞춰져 들어오는 경우가 대부분이다.
    paddle_use_doc_orientation_classify: bool = False
    paddle_use_doc_unwarping: bool = True
    # 실영수증 3장(GS25/영수증2/영수증3)에서 끄고 켜서 비교한 결과 식품/상품 줄에는
    # 손실이 없었고(영수증2는 오히려 더 깔끔한 boilerplate 텍스트) 5~9% 더 빨랐다.
    paddle_use_textline_orientation: bool = False
    paddle_det_model_name: str | None = None
    paddle_det_limit_side_len: int | None = None
    # PaddleOCR enable_hpi=True(전체 자동 선택)는 PP-OCRv5_server_det에서 paddle_enable_mkldnn과
    # 동일한 oneDNN/PIR 비호환 크래시를 냈다(paddle 3.3.1 확인됨). det·rec 두 모델만 이 값으로
    # 강제하고 나머지는 HPI 안전 자동설정에 맡기면 크래시 없이 속도만 얻는다. 예: "onnxruntime".
    # 비워두면 HPI를 전혀 쓰지 않는다(기존 paddleocr.PaddleOCR() 경로).
    paddle_hpi_backend: str | None = None
    paddle_rec_model_name: str | None = None
    # HPI(onnxruntime) det·rec 모델의 CPU 스레드 수. PaddleX 기본값은 10인데 이 EC2는 4코어라
    # 스레드끼리 코어를 뺏는다 — 코어 수(4)로 맞추니 같은 18장 OCR이 167.6초 → 81.9초(49%),
    # 텍스트 결과는 18/18 동일했다(2026-09-22 실험, 한 프로세스에서 엔진을 번갈아 실행해 비교).
    # 서버 코어 수가 바뀌면 같이 바꾼다.
    paddle_cpu_threads: int = Field(default=4, ge=1, le=64)
    # PaddleX 내장 ONNX Runtime(ultra_infer)은 CPU 메모리 아레나를 끌 방법을 노출하지 않고, 이
    # 아레나가 이미지 크기가 바뀔 때마다 커지기만 해서 메모리가 1GB → 8GB까지 늘다 OOM이 났다.
    # true면 det·rec 모델만 pip onnxruntime 세션(enable_cpu_mem_arena=False)으로 실행한다 —
    # 같은 36장에서 0.9GB → 1.7GB에서 멈췄고 텍스트 결과는 동일, 속도는 스레드 4 기준 차이 3% 이내.
    # 교체에 실패하면(onnxruntime 미설치, PaddleX 내부 구조 변경) 로그만 남기고 내장 엔진을 쓴다.
    paddle_ort_disable_arena: bool = True
    # 엔진 리사이클은 이제 안전장치다. 아레나를 끄면 메모리가 거의 늘지 않아 개수 기준은 기본으로
    # 끄고(0), RSS 기준만 남겨 아레나 교체가 실패했을 때를 대비한다. 리사이클은 엔진 객체를
    # 버리고 다음 요청에서 새로 만든다 — 프로세스 재시작 없이 OCR 쪽 메모리만 회수한다
    # (실측: 6.6GB→522MB). 0 이하면 개수 기준 리사이클을 끈다.
    paddle_engine_recycle_after: int = 0
    # 이미지 크기 편차가 크면 요청 개수만으로는 위험하다(실측: 5번째 요청만에 6.5GB 초과).
    # 매 요청 뒤 실제 RSS(MB)가 이 값을 넘으면 개수와 무관하게 즉시 리사이클한다 — 개수·RSS
    # 둘 중 먼저 도달하는 쪽을 따른다. 0 이하면 RSS 기준을 끈다.
    paddle_engine_recycle_rss_mb: int = 6000

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:e2b"
    ollama_timeout_seconds: float = Field(default=120.0, gt=0)
    ollama_temperature: float = Field(default=0.0, ge=0, le=2)
    ollama_num_predict: int = Field(default=512, ge=32, le=2048)
    ollama_keep_alive: str = "30m"

    database_url: str | None = None
    database_pool_max_size: int = Field(default=5, ge=1, le=50)
    reference_cache_max_size: int = Field(default=2000, ge=1, le=100_000)
    catalog_fuzzy_similarity_threshold: float = Field(default=0.4, ge=0.1, le=1.0)

    callback_url: str | None = None
    callback_api_key: str | None = None
    callback_timeout_seconds: float = Field(default=5.0, gt=0)
    callback_max_attempts: int = Field(default=3, ge=1, le=10)

    pipeline_version: str = "receipt-analysis-0.1.0"
    input_classifier_version: str = "mobilenetv3-small-input-type-unavailable"
    ocr_version: str = "pp-ocrv5-korean-mobile"
    normalizer_version: str = "receipt-normalizer-0.1.0"
    policy_version: str = "receipt-policy-0.1.0"

    @model_validator(mode="after")
    # 운영 인증키·S3 버킷·OCR backend 조합을 검증하고 유효한 설정 자신을 반환한다.
    def validate_production_settings(self) -> "Settings":
        if self.callback_url and not self.callback_api_key:
            raise ValueError("CALLBACK_URL을 사용하려면 CALLBACK_API_KEY가 필요합니다")
        if self.callback_api_key and not self.callback_url:
            raise ValueError("CALLBACK_API_KEY를 사용하려면 CALLBACK_URL이 필요합니다")
        if self.app_env == "production" and not self.internal_api_key:
            raise ValueError("production에서는 INTERNAL_API_KEY가 필요합니다")
        if self.app_env == "production" and not self.callback_url:
            raise ValueError("production에서는 CALLBACK_URL이 필요합니다")
        if self.app_env == "production" and not self.callback_api_key:
            raise ValueError("production에서는 CALLBACK_API_KEY가 필요합니다")
        if self.image_source == "s3" and not self.s3_bucket:
            raise ValueError("IMAGE_SOURCE=s3이면 S3_BUCKET이 필요합니다")
        if self.ocr_backend == "stub" and self.app_env == "production":
            raise ValueError("production에서는 OCR_BACKEND=stub을 사용할 수 없습니다")
        if self.inference_backend == "remote" and not self.remote_inference_url:
            raise ValueError("INFERENCE_BACKEND=remote이면 REMOTE_INFERENCE_URL이 필요합니다")
        if self.app_env == "production" and self.inference_backend == "remote" and not self.remote_inference_api_key:
            raise ValueError("production 원격 추론에는 REMOTE_INFERENCE_API_KEY가 필요합니다")
        if self.inference_backend == "remote" and self.image_source == "local" and not self.remote_image_base_url:
            raise ValueError("로컬 이미지 소스의 원격 추론에는 REMOTE_IMAGE_BASE_URL이 필요합니다")
        labels = [label.strip() for label in self.input_classifier_labels.split(",") if label.strip()]
        if set(labels) != {"RECEIPT", "PRODUCT", "OTHER"} or len(labels) != 3:
            raise ValueError("INPUT_CLASSIFIER_LABELS에는 RECEIPT, PRODUCT, OTHER를 각각 한 번씩 지정해야 합니다")
        return self


@lru_cache
# 환경변수와 .env를 읽은 Settings를 프로세스 내 캐시하여 반환한다.
def get_settings() -> Settings:
    return Settings()
