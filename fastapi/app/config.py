import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
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
    # 분석 파이프라인을 동시에 몇 개까지 처리할지. 추론은 GPU Worker가 맡으므로 EC2에서는 Worker
    # 응답 대기와 DB 조회만 겹친다. Worker는 OCR·Gemma를 내부 세마포어로 직렬화하므로 값을 크게
    # 늘려도 처리량은 늘지 않고 Worker 쪽 대기만 길어진다.
    analysis_consumer_count: int = Field(default=2, ge=1, le=16)
    analysis_result_ttl_seconds: int = Field(default=3600, ge=60)
    analysis_result_max_entries: int = Field(default=1000, ge=1)
    analysis_retry_count: int = Field(default=1, ge=0, le=3)
    analysis_retry_delay_seconds: float = Field(default=1.0, ge=0, le=30)

    remote_inference_url: str | None = None
    remote_inference_api_key: str | None = None
    remote_inference_timeout_seconds: float = Field(default=180.0, gt=0)
    remote_image_base_url: str | None = None
    runpod_api_key: str | None = None
    runpod_pod_name_prefix: str | None = None
    runpod_template_id: str | None = None
    runpod_worker_port: int = Field(default=9000, ge=1, le=65535)
    runpod_discovery_ttl_seconds: float = Field(default=60.0, ge=5, le=3600)

    image_source: Literal["s3", "local"] = "local"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_allowed_prefix: str = "uploads/"

    database_url: str | None = None
    recipe_cache_ttl_seconds: float = Field(default=300, ge=1, le=3600)
    recipe_ai_enabled: bool = True
    recipe_ai_timeout_seconds: float = Field(default=45, ge=1, le=120)
    recipe_ai_max_candidates: int = Field(default=12, ge=3, le=20)
    recipe_food_safety_api_key: str | None = Field(default=None, repr=False)
    recipe_korean_archive_api_key: str | None = Field(default=None, repr=False)
    # 원문 저장·재배포 조건을 확인한 운영자만 활성화한다. 키만 넣어도 자동으로 켜지지 않는다.
    recipe_food_safety_storage_approved: bool = False
    recipe_tavily_api_key: str | None = Field(default=None, repr=False, validation_alias=AliasChoices("TAVILY_API_KEY", "RECIPE_TAVILY_API_KEY", "recipe_tavily_api_key"))
    recipe_tavily_max_attempts: int = Field(default=1, ge=1, le=2)
    recipe_tavily_approved_domains: list[str] = Field(default_factory=list, max_length=20)

    # 프로토콜·경로·와일드카드·내부 주소를 승인 호스트에 섞지 못하게 한다.
    @field_validator("recipe_tavily_approved_domains")
    @classmethod
    def validate_recipe_domains(cls, domains: list[str]) -> list[str]:
        result = set()
        for domain in domains:
            host = domain.strip().lower()
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host) or host.endswith((".local", ".internal", ".localhost")):
                raise ValueError("승인된 공개 호스트 이름만 허용합니다.")
            result.add(host)
        return sorted(result)
    database_pool_max_size: int = Field(default=5, ge=1, le=50)
    reference_cache_max_size: int = Field(default=2000, ge=1, le=100_000)
    # 여러 FastAPI 프로세스 중 한 곳에서 개인 보정이 갱신돼도 다른 프로세스가 이 시간 안에
    # PostgreSQL을 다시 조회하도록 상품 카탈로그 캐시에 만료 시간을 둔다.
    product_catalog_cache_ttl_seconds: float = Field(default=60.0, ge=1, le=3600)
    # 낮은 임계값은 주소·생활용품·깨진 OCR 문자열을 이름이 비슷한 식품으로 끌어온다.
    # 0.55부터 시작해 홀드아웃 Precision/Recall을 함께 보며 조정한다.
    catalog_fuzzy_similarity_threshold: float = Field(default=0.55, ge=0.1, le=1.0)

    callback_url: str | None = None
    callback_api_key: str | None = None
    callback_timeout_seconds: float = Field(default=5.0, gt=0)
    callback_max_attempts: int = Field(default=3, ge=1, le=10)

    # 응답 modelTrace에 기록하는 버전 정보. 모델 자체는 GPU Worker에서 실행되므로 Worker 구성이
    # 바뀌면 이 값들도 함께 맞춘다.
    pipeline_version: str = "receipt-analysis-0.1.0"
    ollama_model: str = "gemma4:e2b"
    input_classifier_version: str = "mobilenetv3-small-input-type-v1"
    ocr_version: str = "pp-ocrv5-korean-mobile"
    normalizer_version: str = "receipt-normalizer-0.1.0"
    policy_version: str = "receipt-policy-0.1.0"

    @model_validator(mode="after")
    # 운영 인증키·S3 버킷·원격 GPU Worker 조합을 검증하고 유효한 설정 자신을 반환한다.
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
        has_runpod_discovery = bool(self.runpod_api_key and self.runpod_pod_name_prefix)
        if self.app_env == "production" and not self.remote_inference_url and not has_runpod_discovery:
            raise ValueError(
                "production에서는 REMOTE_INFERENCE_URL 또는 "
                "RUNPOD_API_KEY와 RUNPOD_POD_NAME_PREFIX가 필요합니다"
            )
        if bool(self.runpod_api_key) != bool(self.runpod_pod_name_prefix):
            raise ValueError("Runpod 자동 탐색에는 RUNPOD_API_KEY와 RUNPOD_POD_NAME_PREFIX가 모두 필요합니다")
        if self.app_env == "production" and not self.remote_inference_api_key:
            raise ValueError("production에서는 REMOTE_INFERENCE_API_KEY가 필요합니다")
        if (self.remote_inference_url or has_runpod_discovery) and self.image_source == "local" and not self.remote_image_base_url:
            raise ValueError("로컬 이미지 소스로 GPU Worker를 호출하려면 REMOTE_IMAGE_BASE_URL이 필요합니다")
        return self


@lru_cache
# 환경변수와 .env를 읽은 Settings를 프로세스 내 캐시하여 반환한다.
def get_settings() -> Settings:
    return Settings()
