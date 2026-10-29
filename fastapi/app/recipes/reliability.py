"""레시피 판단의 오류 격리와 원문 기준 취향 판정 메모리 캐시."""

import hashlib
import json
import logging
from collections import OrderedDict
from time import monotonic

import httpx

from app.errors import AppError

logger = logging.getLogger(__name__)

# 입력/인증/모델 출력 오류는 요청에 격리하고 연결 장애와 과부하만 일시 장애로 분류한다.
def transient_failure(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            error = exc.response.json().get("error", {})
            if isinstance(error, dict) and error.get("retryable") is False:
                return False
        except (ValueError, AttributeError):
            pass
        return exc.response.status_code in {429, 502, 503, 504}
    if isinstance(exc, AppError):
        return exc.retryable and exc.status_code in {429, 502, 503, 504}
    return isinstance(exc, (TimeoutError, httpx.TransportError))


class RecipeCircuit:
    # 연속 장애 3회 뒤 30초 열고, 시간이 지나면 복구 시험 하나만 허용한다.
    def __init__(self, threshold: int = 3, cooldown: float = 30) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.open_until = 0.0
        self.probing = False
        self.generation = 0

    # 호출 입장 시 세대 번호를 반환한다. 열린 회로/진행 중인 복구 시험이면 None이다.
    def acquire(self) -> int | None:
        if self.open_until:
            if monotonic() < self.open_until or self.probing:
                return None
            self.probing = True
        return self.generation

    # 이전 세대의 늦은 응답은 새 회로 상태를 덮지 못하게 한다.
    def finish(self, ticket: int, transient: bool = False) -> None:
        if ticket != self.generation:
            return
        if transient:
            self.failures += 1
            if self.probing or self.failures >= self.threshold:
                self.open_until = monotonic() + self.cooldown
                self.generation += 1
                logger.warning("recipe_ai_circuit_open consecutive_failures=%d cooldown_seconds=%.1f", self.failures, self.cooldown)
        else:
            if self.open_until:
                logger.info("recipe_ai_circuit_recovered")
            self.failures = 0
            self.open_until = 0.0
        self.probing = False

    # 작업 취소는 장애 횟수에 넣지 않고 복구 시험 점유만 해제한다.
    def cancel(self, ticket: int) -> None:
        if ticket == self.generation:
            self.probing = False


class PreferenceDecisionCache:
    # 레시피/태그 집합/모델/프롬프트/정책이 같은 검증 판정을 10분간 최대 512개 보관한다.
    def __init__(self, max_size: int = 512, ttl: float = 600) -> None:
        self.max_size, self.ttl = max_size, ttl
        self.entries: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()

    # 수량/재고 ID/후보 순서 대신 실제 판단 원문과 버전을 해시한다.
    def key(self, row: dict, tags: list[str], model: str, prompt: str, policy: str) -> str:
        snapshot = [row["source"], row["sourceRecipeId"], row.get("sourceRevision"), row["name"],
                    row["ingredients"], row["steps"], sorted(tags), model, prompt, policy]
        return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # 빈 dict도 정상적인 적합 근거 없음 판정이다. None은 캐시 미스다.
    def get(self, key: str) -> dict[str, str] | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        if monotonic() - entry[0] >= self.ttl:
            self.entries.pop(key, None)
            return None
        self.entries.move_to_end(key)
        return dict(entry[1])

    # 동시 평가가 겹쳐도 먼저 검증된 판정을 유지한다. 실패/UNKNOWN은 호출자가 넣지 않는다.
    def remember(self, key: str, evidence: dict[str, str]) -> dict[str, str]:
        existing = self.get(key)
        if existing is not None:
            return existing
        self.entries[key] = (monotonic(), dict(evidence))
        while len(self.entries) > self.max_size:
            self.entries.popitem(last=False)
        return dict(evidence)
