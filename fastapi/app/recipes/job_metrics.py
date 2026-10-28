"""사진 분석 지표와 분리된 레시피 큐/결과/콜백 지표."""
from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("dameokja_recipe_requests_total", "레시피 접수 결과", ["outcome"])
QUEUE = Gauge("dameokja_recipe_queue_size", "레시피 대기 작업 수")
ACTIVE = Gauge("dameokja_recipe_active", "처리 중 레시피 작업 수")
WAIT = Histogram("dameokja_recipe_queue_wait_seconds", "레시피 큐 대기시간", buckets=(.1, 1, 5, 10, 30, 60, 120, 300))
DURATION = Histogram("dameokja_recipe_processing_seconds", "레시피 처리시간", ["status"], buckets=(.1, 1, 5, 10, 30, 60, 120, 300))
RESULTS = Counter("dameokja_recipe_results_total", "레시피 최종 결과", ["status"])
CALLBACKS = Counter("dameokja_recipe_callbacks_total", "레시피 콜백 시도", ["outcome"])
