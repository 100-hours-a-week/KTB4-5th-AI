from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

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


# 현재 프로세스의 Prometheus exposition payload와 Content-Type을 반환한다.
def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
