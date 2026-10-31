"""원천 하나의 파싱 / 추출 / 검증과 재시도 상태를 관리한다. 추천 순위와 DB 반입은 결정하지 않는다."""

import asyncio
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, replace
from time import monotonic

import httpx

from app.config import Settings
from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.schemas import Recipe, Step
from app.recipes.source_parsing import parse_failure_reason, parse_web_recipe
from app.recipes.tavily import DiscoveryHit
from app.recipes.web_extraction import source_servings, source_text, validate_extraction

PARSER_VERSION = "recipe-source-v2"


@dataclass
class SourceOutcome:
    recipe: Recipe | None = None
    failure: str | None = None
    unavailable: bool = False
    calls: int = 0
    duration_ms: float = 0
    reused: bool = False
    deferred: bool = False


@dataclass
class SourceFlight:
    task: asyncio.Task[SourceOutcome]
    waiters: int = 0


class RecipeSourceProcessor:
    # 프로세스 메모리만 사용한다. 원천 지문 / 처리 버전이 바뀌면 이전 실패와 성공을 재사용하지 않는다.
    def __init__(self, settings: Settings, reasoner: RecipeReasoner | None = None) -> None:
        self.settings, self.reasoner = settings, reasoner
        self.entries: OrderedDict[str, tuple[float, SourceOutcome]] = OrderedDict()
        self.flights: dict[str, SourceFlight] = {}

    # 같은 원문 처리를 합친다. 모든 대기자가 취소되면 모델 호출도 취소해 고아 작업을 남기지 않는다.
    async def process(self, hit: DiscoveryHit, allow_model: bool, timeout_seconds: float) -> SourceOutcome:
        if timeout_seconds <= 0:
            return SourceOutcome(failure="SOURCE_BUDGET_EXHAUSTED", unavailable=True)
        clean = source_text(hit.rawContent or "")
        raw_hash = hashlib.sha256((hit.rawContent or "").encode()).hexdigest()
        key = hashlib.sha256(json.dumps([hit.url, hit.title, raw_hash, self.settings.ollama_model, PROMPT_VERSION, PARSER_VERSION], ensure_ascii=False).encode()).hexdigest()
        entry = self.entries.get(key)
        if entry and monotonic() < entry[0]:
            self.entries.move_to_end(key)
            saved = entry[1]
            return replace(saved, recipe=saved.recipe.model_copy(deep=True) if saved.recipe else None, calls=0, duration_ms=0, reused=True, deferred=saved.recipe is None)
        self.entries.pop(key, None)
        # 모델 실행 예산이 없을 때 실패를 오래 저장해 이후 추출 기회를 없애지 않는다.
        flight_key = key + (":model" if allow_model else ":parse")
        flight = self.flights.get(flight_key)
        owner = flight is None
        if flight is None:
            flight = SourceFlight(asyncio.create_task(self._process_and_remember(key, hit, clean, allow_model, timeout_seconds)))
            self.flights[flight_key] = flight
        flight.waiters += 1
        try:
            async with asyncio.timeout(timeout_seconds):
                outcome = await asyncio.shield(flight.task)
            return replace(outcome, recipe=outcome.recipe.model_copy(deep=True) if outcome.recipe else None,
                           calls=outcome.calls if owner else 0, duration_ms=outcome.duration_ms if owner else 0,
                           reused=not owner, deferred=not owner and outcome.recipe is None)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    flight.task.cancel()
                    try:
                        await flight.task
                    except asyncio.CancelledError:
                        pass
                self.flights.pop(flight_key, None)

    # 성공과 원문 거부 상태는 재사용하고 통신 / 모델 장애는 짧은 재시도 유예만 남긴다.
    async def _process_and_remember(self, key: str, hit: DiscoveryHit, text: str, allow_model: bool, timeout_seconds: float) -> SourceOutcome:
        outcome = await self._materialize(hit, text, allow_model, timeout_seconds)
        if outcome.failure == "SOURCE_BUDGET_EXHAUSTED":
            return outcome
        if outcome.recipe:
            ttl = self.settings.recipe_cache_ttl_seconds
        elif outcome.unavailable:
            ttl = 15
        elif outcome.calls or outcome.failure in {"EMPTY_BODY", "MIXED_BATCH", "EXTRACTION_INPUT_TOO_LARGE"}:
            ttl = min(60, self.settings.recipe_cache_ttl_seconds)
        else:
            return outcome
        self.entries[key] = (monotonic() + ttl, replace(outcome, recipe=outcome.recipe.model_copy(deep=True) if outcome.recipe else None))
        self.entries.move_to_end(key)
        while len(self.entries) > 64:
            self.entries.popitem(last=False)
        return outcome

    # 파서를 우선하고 남은 시간 안에서 모델 추출을 한 번 수행한다. 원문에 없는 재료나 수량은 생성하지 않는다.
    async def _materialize(self, hit: DiscoveryHit, text: str, allow_model: bool, timeout_seconds: float) -> SourceOutcome:
        recipe = parse_web_recipe(hit)
        if recipe:
            return SourceOutcome(recipe=recipe)
        failure = parse_failure_reason(text)
        if failure in {"MIXED_BATCH", "EMPTY_BODY"} or not allow_model or not self.reasoner or not self.settings.recipe_ai_enabled:
            return SourceOutcome(failure=failure)
        if len(text.splitlines()) > 2000:
            return SourceOutcome(failure="EXTRACTION_INPUT_TOO_LARGE")
        if len(text.encode()) > 24000:
            return SourceOutcome(failure="EXTRACTION_INPUT_TOO_LARGE")
        if timeout_seconds < 3 + self.settings.recipe_worker_transport_margin_seconds:
            return SourceOutcome(failure="SOURCE_BUDGET_EXHAUSTED", unavailable=True)
        started = monotonic()
        outcome = SourceOutcome(calls=1)
        try:
            budget = min(15.0, timeout_seconds)
            async with asyncio.timeout(min(16.0, timeout_seconds)):
                extracted = await self.reasoner.extract({"sourceTitle": hit.title, "sourceText": text, "executionBudgetSeconds": budget})
            verified = validate_extraction(extracted, text, hit.title)
            if not verified:
                outcome.failure = "MIXED_BATCH" if extracted.status == "MIXED_BATCH" else "MODEL_SOURCE_EVIDENCE_REJECTED"
                return outcome
            recipe = Recipe(source="TAVILY_WEB", sourceRecipeId=hashlib.sha256(hit.url.encode()).hexdigest(),
                            sourceRevision=hashlib.sha256((hit.rawContent or "").encode()).hexdigest(), sourceUrl=hit.url,
                            name=verified["name"], baseServings=source_servings(text), ingredients=verified["ingredients"],
                            steps=[Step(stepNo=index, instruction=value) for index, value in enumerate(verified["steps"], 1)])
            outcome.recipe = recipe
            return outcome
        except (AppError, httpx.HTTPError, ValueError, TypeError, TimeoutError):
            outcome.failure, outcome.unavailable = "MODEL_EXTRACTION_UNAVAILABLE", True
            return outcome
        finally:
            outcome.duration_ms = (monotonic() - started) * 1000
