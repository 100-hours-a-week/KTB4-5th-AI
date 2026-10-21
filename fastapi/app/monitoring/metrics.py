from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from app.domain.models import PipelineDiagnostics

ANALYSIS_REQUESTS = Counter(
    "dameokja_analysis_requests_total",
    "분석 접수 결과 수",
    ("outcome",),
)
ANALYSIS_QUEUE_SIZE = Gauge("dameokja_analysis_queue_size", "현재 FastAPI 분석 대기열 크기")
ANALYSIS_QUEUE_OLDEST_WAIT = Gauge(
    "dameokja_analysis_queue_oldest_wait_seconds",
    "현재 대기열에서 가장 오래 기다린 작업의 대기 시간",
)
ANALYSIS_ACTIVE = Gauge("dameokja_analysis_active", "현재 처리 중인 분석 수")
ANALYSIS_STORE_SIZE = Gauge("dameokja_analysis_store_size", "인메모리 분석 결과 저장 건수")
ANALYSIS_QUEUE_WAIT = Histogram(
    "dameokja_analysis_queue_wait_seconds",
    "접수 후 소비자가 처리를 시작할 때까지 걸린 시간",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
ANALYSIS_DURATION = Histogram(
    "dameokja_analysis_duration_seconds",
    "큐 대기를 제외한 분석 처리 시간",
    ("status",),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
ANALYSIS_RESULTS = Counter("dameokja_analysis_results_total", "최종 분석 결과 수", ("status",))
ANALYSIS_ITEMS = Histogram(
    "dameokja_analysis_item_count",
    "완료된 분석 한 건에서 반환한 상품 수",
    buckets=(0, 1, 2, 3, 5, 10, 20, 50),
)
ITEM_DISPLAY_STATUS = Counter(
    "dameokja_analysis_items_total",
    "상품 표시 상태별 반환 수",
    ("display_status",),
)
CALLBACK_ATTEMPTS = Counter(
    "dameokja_callback_attempts_total",
    "백엔드 콜백 시도 결과 수",
    ("outcome",),
)
CALLBACK_DURATION = Histogram(
    "dameokja_callback_duration_seconds",
    "백엔드 콜백 HTTP 요청 시간",
    ("outcome",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
REMOTE_WORKER_STAGE_DURATION = Histogram(
    "dameokja_remote_worker_stage_duration_seconds",
    "RunPod Worker 단계별 대기 및 처리 시간",
    ("stage",),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
REMOTE_LLM_TOKENS = Counter(
    "dameokja_remote_llm_tokens_total",
    "RunPod LLM 누적 토큰 수",
    ("type",),
)
REMOTE_LLM_THROUGHPUT = Histogram(
    "dameokja_remote_llm_throughput_tokens_per_second",
    "RunPod LLM 프롬프트·생성 토큰 처리량",
    ("type",),
    buckets=(1, 5, 10, 20, 50, 100, 200, 500, 1000),
)
REMOTE_LLM_LOAD_DURATION = Histogram(
    "dameokja_remote_llm_model_load_duration_seconds",
    "RunPod Ollama 모델 로드 시간",
    buckets=(0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10, 30),
)

_REMOTE_MILLISECOND_STAGES = {
    "worker_queue_wait_ms": "worker_queue_wait",
    "download_ms": "download",
    "input_classifier_ms": "input_classifier",
    "ocr_queue_wait_ms": "ocr_queue_wait",
    "ocr_ms": "ocr",
    "product_detection_ms": "product_detection",
    "llm_queue_wait_ms": "llm_queue_wait",
    "llm_ms": "llm",
    "worker_total_ms": "worker_total",
}


# RunPod 응답에 포함된 단계 시간과 LLM 토큰 진단값을 FastAPI Prometheus 지표에 기록한다.
def observe_remote_diagnostics(diagnostics: PipelineDiagnostics) -> None:
    for field_name, stage in _REMOTE_MILLISECOND_STAGES.items():
        value = getattr(diagnostics, field_name)
        if _is_non_negative_number(value):
            REMOTE_WORKER_STAGE_DURATION.labels(stage=stage).observe(value / 1000)

    _observe_remote_tokens(
        "prompt",
        diagnostics.llm_prompt_tokens,
        diagnostics.llm_prompt_duration_ns,
    )
    _observe_remote_tokens(
        "completion",
        diagnostics.llm_completion_tokens,
        diagnostics.llm_eval_duration_ns,
    )
    if _is_non_negative_number(diagnostics.llm_load_duration_ns):
        REMOTE_LLM_LOAD_DURATION.observe(diagnostics.llm_load_duration_ns / 1_000_000_000)


# 토큰 수와 처리시간이 유효할 때 누적 토큰 및 초당 처리량을 기록한다.
def _observe_remote_tokens(token_type: str, tokens: object, duration_ns: object) -> None:
    if not _is_non_negative_number(tokens):
        return
    REMOTE_LLM_TOKENS.labels(type=token_type).inc(tokens)
    if _is_positive_number(duration_ns):
        REMOTE_LLM_THROUGHPUT.labels(type=token_type).observe(tokens / (duration_ns / 1_000_000_000))


# bool을 제외한 0 이상의 정수·실수인지 반환한다.
def _is_non_negative_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


# bool을 제외한 0보다 큰 정수·실수인지 반환한다.
def _is_positive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


# 현재 프로세스의 Prometheus exposition payload와 Content-Type을 반환한다.
def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
