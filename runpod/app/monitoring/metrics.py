from collections.abc import Mapping
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

WORKER_REQUESTS = Counter("dameokja_worker_requests_total", "GPU Worker 추론 요청 결과 수", ("status",))
WORKER_INFLIGHT = Gauge("dameokja_worker_inflight", "현재 처리 중인 GPU Worker 요청 수")
WORKER_STAGE_DURATION = Histogram(
    "dameokja_worker_stage_duration_seconds",
    "GPU Worker 단계별 대기 및 처리 시간",
    ("stage",),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
LLM_TOKENS = Counter("dameokja_llm_tokens_total", "LLM 누적 토큰 수", ("type",))
LLM_THROUGHPUT = Histogram(
    "dameokja_llm_throughput_tokens_per_second",
    "LLM 프롬프트·생성 토큰 처리량",
    ("type",),
    buckets=(1, 5, 10, 20, 50, 100, 200, 500, 1000),
)
LLM_LOAD_DURATION = Histogram(
    "dameokja_llm_model_load_duration_seconds",
    "Ollama가 보고한 모델 로드 시간",
    buckets=(0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10, 30),
)

_MILLISECOND_STAGES = {
    "workerQueueWaitMs": "worker_queue_wait",
    "downloadMs": "download",
    "inputClassifierMs": "input_classifier",
    "ocrQueueWaitMs": "ocr_queue_wait",
    "ocrMs": "ocr",
    "productDetectionMs": "product_detection",
    "llmQueueWaitMs": "llm_queue_wait",
    "llmMs": "llm",
    "workerTotalMs": "worker_total",
}


# 한 요청의 진단값을 단계 시간·토큰·처리량 지표로 기록한다.
def observe_diagnostics(diagnostics: Mapping[str, Any]) -> None:
    for key, stage in _MILLISECOND_STAGES.items():
        value = diagnostics.get(key)
        if isinstance(value, int | float) and value >= 0:
            WORKER_STAGE_DURATION.labels(stage=stage).observe(value / 1000)

    prompt_tokens = diagnostics.get("llmPromptTokens")
    completion_tokens = diagnostics.get("llmCompletionTokens")
    prompt_duration_ns = diagnostics.get("llmPromptDurationNs")
    completion_duration_ns = diagnostics.get("llmEvalDurationNs")
    load_duration_ns = diagnostics.get("llmLoadDurationNs")

    _observe_tokens("prompt", prompt_tokens, prompt_duration_ns)
    _observe_tokens("completion", completion_tokens, completion_duration_ns)
    if isinstance(load_duration_ns, int | float) and load_duration_ns >= 0:
        LLM_LOAD_DURATION.observe(load_duration_ns / 1_000_000_000)


# 토큰 수와 나노초 단위 처리 시간을 받아 누적량 및 초당 처리량을 기록한다.
def _observe_tokens(token_type: str, tokens: object, duration_ns: object) -> None:
    if not isinstance(tokens, int | float) or tokens < 0:
        return
    LLM_TOKENS.labels(type=token_type).inc(tokens)
    if isinstance(duration_ns, int | float) and duration_ns > 0:
        LLM_THROUGHPUT.labels(type=token_type).observe(tokens / (duration_ns / 1_000_000_000))


# 현재 프로세스의 Prometheus exposition payload와 Content-Type을 반환한다.
def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
