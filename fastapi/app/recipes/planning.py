"""검증된 레시피의 기본 추천을 구성하고 AI 평가로 취향과 순서를 조정한다."""

import asyncio
import json
import logging
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.errors import AppError
from app.logging.recipe_trace import latency
from app.recipes.ai import RecipeReasoner, last_usage
from app.recipes.ai_contract import PROMPT_VERSION, RankResult, TasteProfile
from app.recipes.candidate_pool import representative_candidates
from app.recipes.context import variation_key
from app.recipes.meal_quality import frying_steps, taste_context, weak_spicy_basis
from app.recipes.preferences import valid_preference_evidence
from app.recipes.recipe_attributes import compact_attributes
from app.recipes.reliability import PreferenceDecisionCache, RecipeCircuit, SituationDecisionCache, transient_failure
from app.recipes.retrieval import canonical, missing_named_ingredients
from app.recipes.schemas import RecommendationRequest
from app.recipes.selection import candidate_priority, select_recommendations
from app.recipes.suitability import assess_suitability
from app.recipes.taste_profiles import preference_decisions, uncertain_unmatched_tags

logger = logging.getLogger(__name__)
AI_ERRORS = (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError)
POLICY_VERSION = "recipe-rerank-v25"


# 기존 내부 호출 경로를 보존하되 최종 선정 책임은 단일 모듈로 위임한다.
def cover_expiring(rows: list[dict], request: RecommendationRequest) -> list[dict]:
    return select_recommendations(rows, request)


# 기존 도구의 순서 보존용 경로다. 운영 추천은 select_recommendations에서 다양성까지 한 번에 계산한다.
def diversify(rows: list[dict], recent_ids: list[str]) -> list[dict]:
    return rows


class MealRanker:
    # DB/분량 검증 뒤 생성한다. 원격 호출은 슬롯 대기와 재시도를 포함한 전체 시간 상한을 공유한다.
    def __init__(self, reasoner: RecipeReasoner, timeout: float, slots: asyncio.Semaphore, expected_model: str, max_input_bytes: int = 16000) -> None:
        self.reasoner, self.timeout, self.slots = reasoner, timeout, slots
        self.expected_model = expected_model
        self.model: str | None = None
        self.calls = 0
        self.unavailable = False
        self.warnings: list[str] = []
        self.preference_matches: set[tuple[str, str]] = set()
        self.preference_evidence: dict[tuple[str, str], str] = {}
        self.preference_tag_evidence: dict[tuple[str, str], dict[str, str]] = {}
        self.rejected_preference_tags: dict[tuple[str, str], set[str]] = {}
        self.rejected_preference_identities: set[tuple[str, str]] = set()
        self.evaluated_identities: set[tuple[str, str]] = set()
        self.max_input_bytes = max_input_bytes
        self.input_bytes = 0
        self.candidate_count = 0
        self.duration_ms = 0.0
        self.decisions = PreferenceDecisionCache()
        self.circuit = RecipeCircuit()
        self.preference_cache_hits = 0
        self._cached_rows: dict[tuple[str, str], dict[str, str]] = {}
        # 모델이 후보별로 고른 상황 이유 코드다. 서버 검증 전 값이며 그대로 사용자에게 보이지 않는다.
        self.context_factors: dict[tuple[str, str], list[str]] = {}
        self.situations = SituationDecisionCache()
        self.situation_cache_hits = 0
        # RANK 호출별 Worker 모델 사용량(토큰 / 프롬프트 처리 / 생성 시간)과 FastAPI에서 잰 왕복 시간이다.
        self.rank_usage: list[dict] = []

    # 현재 평가 창 밖이라도 같은 원문 / 취향의 검증된 판정이 있으면 후보 풀에 적용한다.
    def hydrate(self, context: dict, rows: list[dict]) -> None:
        tags = context.get("preferenceTags", [])
        if not tags:
            return
        for row in rows:
            identity = (row["source"], row["sourceRecipeId"])
            if identity in self.evaluated_identities:
                continue
            evidence = self.decisions.get(self.decisions.key(row, tags, self.expected_model, PROMPT_VERSION, POLICY_VERSION))
            if evidence is None:
                continue
            self.evaluated_identities.add(identity)
            self.preference_cache_hits += 1
            if evidence:
                self.preference_matches.add(identity)
                self.preference_tag_evidence[identity] = evidence
                self.preference_evidence[identity] = next(iter(evidence.values()))
        # 원천별 평가를 현재 태그에 연결한다. UNKNOWN을 요청별 모델 주장이나 오래된 캐시로 덮지 않는다.
        for row in rows:
            if row.get("tasteProfile") is None:
                identity = (row["source"], row["sourceRecipeId"])
                if identity in self.evaluated_identities:
                    uncertain = uncertain_unmatched_tags(row, tags) - self.preference_tag_evidence.get(identity, {}).keys()
                    if uncertain:
                        self.rejected_preference_tags.setdefault(identity, set()).update(uncertain)
                continue
            decisions = preference_decisions(TasteProfile.model_validate(row["tasteProfile"]), tags)
            row["_profileTagStatuses"] = {tag: status for tag, (status, _) in decisions.items()}
            identity = (row["source"], row["sourceRecipeId"])
            evidence = self.preference_tag_evidence.setdefault(identity, {})
            for tag, (status, quote) in decisions.items():
                evidence.pop(tag, None)
                if status == "MATCHED" and quote:
                    evidence[tag] = quote
                if status == "UNKNOWN":
                    self.rejected_preference_tags.setdefault(identity, set()).add(tag)
                elif identity in self.rejected_preference_tags:
                    self.rejected_preference_tags[identity].discard(tag)
                    if not self.rejected_preference_tags[identity]:
                        self.rejected_preference_tags.pop(identity)
            if decisions:
                self.evaluated_identities.add(identity)
                self.model = self.expected_model
            if set(tags) <= decisions.keys():
                self.rejected_preference_identities.discard(identity)
            self.preference_matches.discard(identity)
            self.preference_evidence.pop(identity, None)
            if evidence:
                self.preference_matches.add(identity)
                self.preference_evidence[identity] = next(iter(evidence.values()))
        if self.preference_cache_hits:
            self.warnings.append("PREFERENCE_DECISION_CACHE_USED")

    # 원문/태그의 검증 판정을 재사용하며 순위와 재고 소모량은 현재 요청에서 다시 계산한다.
    async def rank(self, context: dict, shortlist: list[dict]) -> list[dict] | None:
        tags = context.get("preferenceTags", [])
        cached, keys = {}, {}
        for row in shortlist if tags else []:
            identity = (row["source"], row["sourceRecipeId"])
            keys[identity] = self.decisions.key(row, tags, self.expected_model, PROMPT_VERSION, POLICY_VERSION)
            evidence = self.decisions.get(keys[identity])
            if evidence is not None:
                cached[identity] = evidence
        self._cached_rows = cached
        self.preference_cache_hits += len(cached)
        # 저장된 원천 평가가 모든 요청 태그를 다루면 같은 원문을 다시 GPU에 보내지 않는다.
        for row in shortlist if tags else []:
            if row.get("tasteProfile") is None:
                continue
            decisions = preference_decisions(TasteProfile.model_validate(row["tasteProfile"]), tags)
            if set(tags) <= decisions.keys():
                cached[(row["source"], row["sourceRecipeId"])] = {tag: quote for tag, (status, quote) in decisions.items()
                                                                 if status == "MATCHED" and quote}
        self._cached_rows = cached
        reused = bool(self.preference_cache_hits)
        # 평가 예산 밖 후보 때문에 같은 창을 반복 추론하지 않는다. 창 밖 후보는 기존 UNKNOWN 정책을 유지한다.
        fitted = shortlist
        contextual = "recommendationContext" in context
        if tags and cached or contextual:
            try:
                _, fitted = pack_rank_input(context, shortlist, self.max_input_bytes)
            except ValueError:
                fitted = []
        preference_ready = not tags or all((row["source"], row["sourceRecipeId"]) in cached for row in fitted)
        situation_keys = {(row["source"], row["sourceRecipeId"]): self.situations.key(row, context["recommendationContext"], self.expected_model,
                                                                                    PROMPT_VERSION, POLICY_VERSION) for row in fitted} if contextual else {}
        situation_cached = {identity: factors for identity, key in situation_keys.items() if (factors := self.situations.get(key)) is not None}
        # 날씨 / 시간대 판단은 취향이 없을 때만 모델 호출 사유가 된다. 취향 요청은 판정이 모두 저장 / 캐시돼 있으면
        # 호출을 생략하고, 취향을 못 찾아 대체 추천할 때는 캐시에 있는 상황 판단만 쓴다.
        situation_required = contextual and not tags
        all_cached = bool(fitted and preference_ready and (len(situation_cached) == len(fitted) if situation_required else tags))
        if all_cached:
            self.preference_matches.clear()
            self.preference_evidence.clear()
            self.preference_tag_evidence.clear()
            self.rejected_preference_tags.clear()
            self.rejected_preference_identities.clear()
            self.evaluated_identities.clear()
            self.unavailable = False
            selected = fitted
            if len(fitted) < len(shortlist):
                self.warnings.append("AI_INPUT_WINDOW_REDUCED")
            self.model = self.expected_model
            self.context_factors = dict(situation_cached)
            self.situation_cache_hits += len(situation_cached)
        elif tags and cached and not situation_required:
            # 저장 평가 / 판정 캐시로 끝난 후보는 GPU에 다시 보내지 않는다. 12개 중 1개만 미평가여도 12개를 모두 보내
            # 요청마다 GPU 왕복 1.9~2.6초가 들었다(dev 실측). 순서는 서버 정책이 정하므로 미평가 후보만 판정한다.
            window = fitted or shortlist
            pending = [row for row in shortlist if (row["source"], row["sourceRecipeId"]) not in cached]
            judged = await self._rank(context, pending)
            selected = None if judged is None else [*judged, *(row for row in window if (row["source"], row["sourceRecipeId"]) in cached)]
        else:
            selected = await self._rank(context, shortlist)
            # 정상 응답이면 평가 창의 모든 후보에 대해 상황 판단을 저장한다. 이유가 없던 후보도 '해당 없음' 판단이다.
            # 취향 요청은 상황 이유를 요청하지 않으므로 빈 결과를 '해당 없음'으로 저장하면 안 된다.
            if selected is not None and situation_required:
                for row in selected:
                    identity = (row["source"], row["sourceRecipeId"])
                    if identity in situation_keys:
                        self.situations.remember(situation_keys[identity], self.context_factors.get(identity, []))
        if selected is not None and not all_cached:
            for row in selected:
                identity = (row["source"], row["sourceRecipeId"])
                uncertain = uncertain_unmatched_tags(row, tags) - self.preference_tag_evidence.get(identity, {}).keys()
                if row.get("tasteProfile") is None and uncertain:
                    self.rejected_preference_tags.setdefault(identity, set()).update(uncertain)
            for identity in self.evaluated_identities:
                # 이미 캐시 / 저장 평가로 판정한 후보는 근거 선택지를 비워 보냈으므로 빈 결과로 덮어쓰지 않는다.
                if (identity in keys and identity not in self._cached_rows and identity not in self.rejected_preference_identities
                        and identity not in self.rejected_preference_tags):
                    cached[identity] = self.decisions.remember(keys[identity], self.preference_tag_evidence.get(identity, {}))
        for identity, evidence in cached.items():
            self.evaluated_identities.add(identity)
            self.rejected_preference_identities.discard(identity)
            self.rejected_preference_tags.pop(identity, None)
            self.preference_matches.discard(identity)
            self.preference_evidence.pop(identity, None)
            self.preference_tag_evidence.pop(identity, None)
            if evidence:
                self.preference_matches.add(identity)
                self.preference_tag_evidence[identity] = evidence
                self.preference_evidence[identity] = next(iter(evidence.values()))
        if reused:
            self.warnings.append("PREFERENCE_DECISION_CACHE_USED")
        if selected is None:
            return None
        for row in selected:
            identity = (row["source"], row["sourceRecipeId"])
            if tags:
                row["preferenceMatch"] = "MATCHED" if identity in self.preference_matches else "UNKNOWN"
                row["preferenceAssessments"] = [{"tag": tag, "status": "MATCHED", "evidence": quote}
                                                for tag, quote in self.preference_tag_evidence.get(identity, {}).items()]
        return sorted(selected, key=eligibility_tier)

    # 후보 전체의 ID 순열만 수용한다. 오류·누락·중복은 기본 추천을 지우지 않고 None으로 반환한다.
    async def _rank(self, context: dict, shortlist: list[dict]) -> list[dict] | None:
        self.preference_matches = set()
        self.preference_evidence = {}
        self.preference_tag_evidence = {}
        self.rejected_preference_tags = {}
        self.rejected_preference_identities = set()
        self.evaluated_identities = set()
        self.context_factors = {}
        self.unavailable = False
        self.model = None
        call_started = None
        ticket = None
        try:
            payload, window = pack_rank_input(context, shortlist, self.max_input_bytes)
            for candidate, row in zip(payload["candidates"], window, strict=True):
                if (row["source"], row["sourceRecipeId"]) in self._cached_rows:
                    candidate["preferenceEvidenceOptions"] = []
            self.input_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
            self.candidate_count = len(window)
            if len(window) < len(shortlist):
                self.warnings.append("AI_INPUT_WINDOW_REDUCED")
            started = monotonic()
            call_started = started
            latency("fastapi_slot_wait_started", budget_seconds=self.timeout)
            async with asyncio.timeout(self.timeout), self.slots:
                latency("fastapi_slot_acquired", wait_ms=round((monotonic()-started)*1000, 3))
                ticket = self.circuit.acquire()
                if ticket is None:
                    self.warnings.append("AI_CIRCUIT_OPEN")
                    return None
                for attempt in range(2):
                    payload["executionBudgetSeconds"] = max(0.1, min(60, self.timeout - (monotonic() - started) - 1))
                    self.input_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
                    self.calls += 1
                    logger.info("recipe_ai_request candidate_count=%d input_bytes=%d attempt=%d budget_seconds=%.2f", len(window), self.input_bytes, attempt + 1, payload["executionBudgetSeconds"])
                    try:
                        last_usage.set(None)
                        calling = monotonic()
                        result, model = await self.reasoner.rank(payload)
                        self.rank_usage.append({"candidates": len(window), "inputBytes": self.input_bytes,
                                                "roundTripMs": round((monotonic() - calling) * 1000, 1), **(last_usage.get() or {})})
                        break
                    except AI_ERRORS as exc:
                        limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429 or isinstance(exc, AppError) and exc.status_code == 429
                        timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
                        if attempt or not transient_failure(exc) or limited or timed_out or self.timeout - (monotonic() - started) <= 2:
                            raise
                        self.warnings.append("AI_TRANSIENT_RETRY")
                        await asyncio.sleep(0.1)
            if not isinstance(result, RankResult) or model != self.expected_model:
                raise ValueError("RECIPE_MODEL_OR_SCHEMA_MISMATCH")
            wanted = set(range(1, len(window) + 1))
            if len(result.candidateIds) != len(window) or set(result.candidateIds) != wanted:
                raise ValueError("INVALID_RECIPE_RANK_PERMUTATION")
            matched_ids = result.preferenceMatchedCandidateIds
            if len(matched_ids) != len(set(matched_ids)) or not set(matched_ids) <= wanted:
                raise ValueError("INVALID_RECIPE_PREFERENCE_IDS")
            if not {item.candidateId for item in result.preferenceEvidence} <= set(matched_ids):
                raise ValueError("INVALID_RECIPE_PREFERENCE_IDS")
            if not {item.candidateId for item in result.contextReasons} <= wanted:
                raise ValueError("INVALID_RECIPE_CONTEXT_REASON_IDS")
            for item in result.contextReasons:
                row = window[item.candidateId - 1]
                self.context_factors.setdefault((row["source"], row["sourceRecipeId"]), list(dict.fromkeys(item.factors)))
            for identifier in matched_ids:
                row = window[identifier - 1]
                identity = (row["source"], row["sourceRecipeId"])
                tags = context.get("preferenceTags", [])
                verified = {}
                for item in result.preferenceEvidence:
                    if item.candidateId != identifier:
                        continue
                    if any(tag not in tags for tag in item.tags):
                        raise ValueError("INVALID_RECIPE_PREFERENCE_IDS")
                    for tag in item.tags:
                        if valid_preference_evidence(row, item.quote, [tag]):
                            verified.setdefault(tag, item.quote)
                        else:
                            self.rejected_preference_tags.setdefault(identity, set()).add(tag)
                            self.warnings.append("AI_PREFERENCE_EVIDENCE_REJECTED")
                if verified:
                    self.preference_matches.add(identity)
                    self.preference_tag_evidence[identity] = verified
                    self.preference_evidence[identity] = next(iter(verified.values()))
                else:
                    self.rejected_preference_identities.add((row["source"], row["sourceRecipeId"]))
                    self.warnings.append("AI_PREFERENCE_EVIDENCE_REJECTED")
            self.evaluated_identities = {(row["source"], row["sourceRecipeId"]) for row in window}
            ordered = [window[identifier - 1] for identifier in result.candidateIds]
            if context.get("preferenceTags"):
                ordered = sorted(ordered, key=lambda row: (row["source"], row["sourceRecipeId"]) not in self.preference_matches)
                for row in ordered:
                    row["preferenceMatch"] = "MATCHED" if (row["source"], row["sourceRecipeId"]) in self.preference_matches else "UNKNOWN"
                    row["preferenceAssessments"] = [{"tag": tag, "status": "MATCHED", "evidence": quote}
                                                    for tag, quote in self.preference_tag_evidence.get((row["source"], row["sourceRecipeId"]), {}).items()]
            # AI가 실제 임박 활용·소비기한·적합성의 검증된 우선순위를 뒤집을 수 없다.
            constrained = sorted(ordered, key=eligibility_tier)
            if constrained != ordered:
                self.warnings.append("AI_ORDER_CONSTRAINED_BY_ELIGIBILITY")
            self.model = model
            self.circuit.finish(ticket)
            ticket = None
            return constrained
        except asyncio.CancelledError:
            if ticket is not None:
                self.circuit.cancel(ticket)
            raise
        except AI_ERRORS as exc:
            self.preference_matches.clear()
            self.preference_evidence.clear()
            self.preference_tag_evidence.clear()
            self.rejected_preference_tags.clear()
            self.rejected_preference_identities.clear()
            self.evaluated_identities.clear()
            self.unavailable = transient_failure(exc)
            if ticket is not None:
                self.circuit.finish(ticket, self.unavailable)
            self.context_factors.clear()
            allowed = {"RECIPE_CONTEXT_TOO_LARGE", "RECIPE_MODEL_OR_SCHEMA_MISMATCH", "INVALID_RECIPE_RANK_PERMUTATION", "INVALID_RECIPE_PREFERENCE_IDS",
                       "INVALID_RECIPE_CONTEXT_REASON_IDS"}
            code = str(exc) if type(exc) is ValueError and str(exc) in allowed else "TIME_BUDGET_EXCEEDED" if isinstance(exc, TimeoutError) else "REMOTE_OR_SCHEMA_ERROR"
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code if isinstance(exc, AppError) else None
            logger.warning("recipe_ai_fallback stage=RANK error_type=%s reason_code=%s transient=%s status=%s prompt=%s", type(exc).__name__, code, self.unavailable, status, PROMPT_VERSION)
            self.warnings.append("AI_UNAVAILABLE_OR_INVALID")
            return None
        finally:
            if call_started is not None:
                latency("rank_finished", total_ms=round((monotonic()-call_started)*1000, 3), calls=self.calls)
                self.duration_ms += (monotonic() - call_started) * 1000


# 사전 후보 정렬과 최종 선정이 같은 정적 우선순위를 사용하도록 위임한다.
def eligibility_tier(row: dict) -> tuple[int | float, ...]:
    return candidate_priority(row)


# 부족한 주재료도 구매 필요 후보로 포함한다. 정확히 보유한 식재료가 없는 계열 후보만 따로 둔다.
def build_baseline(ranked: list[dict], request: RecommendationRequest, confirmation_only: bool = False) -> list[dict]:
    rows = []
    seen = set()
    for original in ranked:
        identity = (original["source"], original["sourceRecipeId"])
        if identity in seen:
            continue
        seen.add(identity)
        row = dict(original)
        facts = row.get("_suitability") or assess_suitability(row, [item.name for item in request.inventory])
        row["_suitability"] = facts
        if confirmation_only:
            if facts["hasFoodMatch"] or not facts.get("hasRelatedFood"):
                continue
        elif not facts["hasFoodMatch"]:
            continue
        urgent = row["usesExpiringIngredients"]
        urgent_main = [item["name"] for item in facts["ingredients"] if item["role"] == "MAIN" and canonical(item["name"]) in {canonical(name) for name in urgent}]
        row["usesExpiringMainIngredients"] = list(dict.fromkeys(urgent_main))
        if "USE_EXPIRING" in request.strategies:
            focus = urgent_main or urgent
            today = datetime.now(ZoneInfo("Asia/Seoul")).date()
            dates = [item.expirationDate for item in request.inventory if canonical(item.name) in {canonical(name) for name in focus}
                     and 0 <= (item.expirationDate - today).days <= 3]
            # 임박 주재료를 먼저 보되, 같은 핵심 활용 그룹에서는 조리 적합성이 날짜보다 앞선다.
            row["_expiryPriority"] = (0 if urgent else 1,)
            row["_expiryDetail"] = (min(dates).toordinal(), -len(set(urgent_main))) if dates else (0, 0)
        strategy = "USE_EXPIRING" if urgent and "USE_EXPIRING" in request.strategies else "MINIMIZE_SHOPPING" if "MINIMIZE_SHOPPING" in request.strategies else "SIMPLE_COOKING"
        essential = [item["ingredientNo"] for item in facts["ingredients"] if item["role"] == "MAIN"]
        row.update(strategy=strategy, essentialIngredientNos=essential,
                   missingEssentialIngredientNos=[item["ingredientNo"] for item in facts["ingredients"] if item["role"] == "MAIN" and item["missing"]])
        row["feasibility"] = "NEEDS_SHOPPING" if row["missingIngredients"] or row["quantityStatus"] == "SHORTAGE" else "QUANTITY_VERIFIED" if row["quantityStatus"] == "SUFFICIENT" else "NEEDS_CHECK"
        row["confirmationReasons"] = ([f"재료 종류·상태 확인: 보유 {', '.join(item['relatedStockNames'])} / 원문 필요 {item['name']}" for item in facts["relatedMain"]]
                                      + [f"역할·보유 여부 확인: {item['name']}" for item in facts["unresolvedMissing"]])
        if confirmation_only:
            row["feasibility"] = "NEEDS_CHECK"
        if row.get("baseServings") is None:
            row["confirmationReasons"].append("원문 기준 인분이 없어 1인분으로 가정해 요청 인분의 필요량을 계산했습니다.")
        if len(row["missingIngredients"]) > 5:
            row["confirmationReasons"].append("추가 식재료가 5종을 초과합니다. 구매 예산 목표는 2만원이나 가격 정보가 없어 충족 여부는 확인이 필요합니다.")
        if weak_spicy_basis(row):
            row["confirmationReasons"].append("고추장만 소량 표기돼 있어 매운 강도를 확정하기 어렵습니다.")
        if frying_steps(row):
            row["confirmationReasons"].append("원문에 튀김 과정이 있어 담백한 취향 적합성은 보수적으로 확인합니다.")
        urgent_text = f"3일 이내 소비기한 재료 {', '.join(urgent)}가 원문에 포함됩니다." if urgent else "이 레시피에 사용되는 임박 재료는 없습니다."
        missing_title = missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"])
        warning = f" 제목에 명시된 미보유 재료: {', '.join(missing_title)}." if missing_title else ""
        if facts["relatedMain"]:
            warning += " 같은 계열 재고가 있지만 부위·가공 상태가 같다고 확정할 수 없어 정확한 재료와 필요량을 확인해야 합니다."
        if facts["unresolvedMissing"]:
            warning += f" 역할·보유 여부 확인 필요: {', '.join(item['name'] for item in facts['unresolvedMissing'])}. 원문 재료를 생략해도 된다는 뜻은 아닙니다."
        if row["missingIngredients"]:
            warning += f" 추가로 준비할 식재료: {', '.join(row['missingIngredients'])}."
        row["reason"] = (f"보유 재료 {len(row['usesInventory'])}종 사용, 추가로 확인할 재료 {len(row['missingIngredients'])}종입니다. "
                         f"{urgent_text} 원문 조리 {len(row['steps'])}단계이며 분량 상태는 {row['quantityStatus']}입니다.{warning}")
        if row["missingIngredients"]:
            row["reason"] += " 가격 정보가 없어 추가 구매 2만원 예산 충족 여부는 미확인입니다."
        rows.append(row)
    rows.sort(key=lambda row: (*eligibility_tier(row), len(row["steps"]) if request.strategies == ["SIMPLE_COOKING"] else 0,
                              len(row["missingIngredients"]), *row.get("_expiryDetail", ()), -row["score"], row["source"], row["sourceRecipeId"]))
    return rows


# 원문 후보를 보존하면서 무취향 요청의 동일 우선순위 후보를 요청별로 섞는다.
def vary_baseline(rows: list[dict], seed: str) -> list[dict]:
    return sorted(rows, key=lambda row: (*eligibility_tier(row), variation_key(row, seed)))


# 순위 판단에 필요한 원문 근거만 압축한다. 실제 응답의 전체 원문 재료와 단계는 자르지 않는다.
def compact_candidates(shortlist: list[dict], brief: bool = False) -> list[dict]:
    result = []
    for index, row in enumerate(shortlist, 1):
        requirements = {r["ingredientNo"]: r for r in row["ingredientRequirements"]}
        roles = {item["ingredientNo"]: item for item in row["_suitability"]["ingredients"]}
        if brief:
            used = {canonical(name) for name in row["usesInventory"]}
            urgent = {canonical(name) for name in row["usesExpiringIngredients"]}
            result.append({"candidateId": index, "name": row["name"],
                           "ingredients": [[item["ingredientNo"], item["name"], roles[item["ingredientNo"]]["role"],
                                            requirements[item["ingredientNo"]]["status"], roles[item["ingredientNo"]]["relatedStockNames"]]
                                           for item in row["ingredients"]],
                           "ownedIngredientNos": [item["ingredientNo"] for item in row["ingredients"] if canonical(item["name"]) in used],
                           "expiringIngredientNos": [item["ingredientNo"] for item in row["ingredients"] if canonical(item["name"]) in urgent],
                           "missingIngredientNos": [item["ingredientNo"] for item in roles.values() if item["missing"]],
                           "eligibilityTier": row["_suitability"]["tier"], "expiryPriority": list(row.get("_expiryPriority", ())),
                           "quantityStatus": row["quantityStatus"],
                           "steps": [step["instruction"][:80] for step in row["steps"][:2]],
                           "stepCount": len(row["steps"]), "detailsReduced": True, "stepsMayBeTruncated": True})
            continue
        ingredients = []
        for item in row["ingredients"]:
            requirement = requirements.get(item["ingredientNo"], {})
            keys = ("status",) if brief else ("requiredAmount", "unit", "shortageAmount", "status")
            role = roles[item["ingredientNo"]]
            ingredients.append({"ingredientNo": item["ingredientNo"], "name": item["name"], "role": role["role"], "roleEvidence": role["roleEvidence"],
                                "relatedStockNames": role["relatedStockNames"],
                                **{key: requirement[key] for key in keys if requirement.get(key) is not None}})
        result.append({"candidateId": index, "name": row["name"], "ingredients": ingredients,
                       "usesInventory": row["usesInventory"], "usesExpiringIngredients": row["usesExpiringIngredients"],
                       "missingIngredients": row["missingIngredients"], "quantityStatus": row["quantityStatus"],
                       "missingNamedIngredients": missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"]),
                       "eligibilityTier": row["_suitability"]["tier"],
                       "expiryPriority": list(row.get("_expiryPriority", ())),
                       "stepCount": len(row["steps"]), "steps": [] if brief else [step["instruction"][:100] for step in row["steps"][:2]],
                       "detailsReduced": brief, "stepsMayBeTruncated": brief or len(row["steps"]) > 2 or any(len(step["instruction"]) > 100 for step in row["steps"])})
    return result


# 원문 응답은 보존하고 AI 평가 창만 입력 예산에 맞춘다. 한 후보조차 못 담으면 명시적으로 실패한다.
def pack_rank_input(context: dict, shortlist: list[dict], max_bytes: int) -> tuple[dict, list[dict]]:
    base = {key: value for key, value in context.items() if key != "pantryIngredientNames"}
    base["inventory"] = [{key: item[key] for key in ("ingredientId", "name", "expirationDate", "storageType") if key in item}
                         for item in context.get("inventory", [])]
    base["inventoryDetailsReduced"] = True
    base["ingredientColumns"] = ["ingredientNo", "name", "role", "quantityStatus", "relatedStockNames"]
    if len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode()) > max_bytes - 64:
        raise ValueError("RECIPE_CONTEXT_TOO_LARGE")
    shortlist = representative_candidates(shortlist, len(shortlist))
    # 남은 재료/분량은 FastAPI가 이미 검증했다. 모델이 빠진 중량을 추측할 필요가 없다.
    tags = context.get("preferenceTags")
    if tags:
        # 취향 요청에서 모델이 하는 일은 원문 근거 판정뿐이다. 최종 순서는 서버가 정하고 날씨 이유도 쓰지 않으므로
        # 재고 / 임박 / 분량 / 요리 분류 열을 빼고 상황 이유 출력을 끈다. GPU 입력 / 출력 토큰이 줄어 응답이 빨라진다.
        base["ingredientColumns"] = ["ingredientNo", "name"]
        base["contextReasonsWanted"] = False
    for count in range(len(shortlist), 0, -1):
        window = shortlist[:count]
        if tags:
            candidates = [{"candidateId": index, "name": row["name"],
                           "ingredients": [[item["ingredientNo"], item["name"]] for item in row["ingredients"]],
                           "steps": [step["instruction"][:80] for step in row["steps"][:2]], "stepCount": len(row["steps"])}
                          for index, row in enumerate(window, 1)]
        else:
            candidates = compact_candidates(window, brief=True)
        payload = {**base, "candidates": candidates, "executionBudgetSeconds": 60.0}
        for candidate, row in zip(payload["candidates"], window, strict=True):
            attributes = compact_attributes(row.get("recipeAttributes"))
            if attributes and not tags:
                candidate["attributes"] = attributes
        if tags:
            for candidate, row in zip(payload["candidates"], window, strict=True):
                candidate["tasteContext"] = taste_context(row)
                quotes = [item["name"] for item in row["ingredients"] if len(item["name"]) <= 80]
                quotes.extend(text[:80] for text in candidate["tasteContext"]["cookingEvidence"])
                candidate["preferenceEvidenceOptions"] = list(dict.fromkeys(quote for quote in quotes
                    if any(valid_preference_evidence(row, quote, [tag]) for tag in context["preferenceTags"])))
        # 실제 HTTP JSON과 같은 공백 없는 직렬화로 검사하고 실행 예산 숫자 변경 여유도 남긴다.
        if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= max_bytes - 64:
            return payload, window
    raise ValueError("RECIPE_CONTEXT_TOO_LARGE")
