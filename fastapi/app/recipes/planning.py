"""검증된 레시피의 기본 추천을 구성하고 AI 평가로 취향과 순서를 조정한다."""

import asyncio
import json
import logging
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION, RankResult
from app.recipes.context import variation_key
from app.recipes.preferences import valid_preference_evidence
from app.recipes.reliability import PreferenceDecisionCache, RecipeCircuit, transient_failure
from app.recipes.retrieval import canonical, missing_named_ingredients
from app.recipes.schemas import RecommendationRequest
from app.recipes.suitability import assess_suitability

logger = logging.getLogger(__name__)
AI_ERRORS = (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError)
POLICY_VERSION = "recipe-rerank-v15"


# 임박 활용 그룹을 보호하고 취향 일치 여부를 먼저 비교한 뒤 재료 분산과 구매 부담을 고려한다.
def cover_expiring(rows: list[dict], request: RecommendationRequest) -> list[dict]:
    if "USE_EXPIRING" not in request.strategies:
        return rows
    dates = {}
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    for item in request.inventory:
        if not 0 <= (item.expirationDate - today).days <= 3:
            continue
        name = canonical(item.name)
        dates[name] = min(dates.get(name, item.expirationDate), item.expirationDate)
    pending, selected, covered = list(rows), [], set()
    group_size = 4 if request.preferenceTags else 2
    while pending and len(selected) < request.limit:
        best = min(eligibility_tier(row)[:group_size] for row in pending)
        group = [row for row in pending if eligibility_tier(row)[:group_size] == best]
        minimum_missing = min(len(row["missingIngredients"]) for row in group)
        fresh = [(row, {canonical(name) for name in row["usesExpiringMainIngredients"]} - covered) for row in group
                 if row.get("preferenceMatch") == "MATCHED" or len(row["missingIngredients"]) <= minimum_missing + 2]
        fresh = [(row, names) for row, names in fresh if names]
        if fresh:
            earliest = min(min(dates[name] for name in names) for _, names in fresh)
            fresh = [(row, names) for row, names in fresh if min(dates[name] for name in names) == earliest]
            preferred = [(row, names) for row, names in fresh if row.get("preferenceMatch") == "MATCHED"] or fresh
            minimum_missing = min(len(row["missingIngredients"]) for row, _ in preferred)
            affordable = [(row, names) for row, names in preferred if len(row["missingIngredients"]) <= minimum_missing + 2]
            row, newly_used = min(affordable, key=lambda pair: (-len(pair[1]), len(pair[0]["missingIngredients"])))
            row = dict(row)
            row["reason"] += f" 임박 재료 분산 활용: {', '.join(sorted(newly_used))}를 앞선 추천과 추가로 활용하도록 선정했습니다."
        else:
            preferred = [row for row in group if row.get("preferenceMatch") == "MATCHED"] or group
            minimum_missing = min(len(row["missingIngredients"]) for row in preferred)
            affordable = [row for row in preferred if len(row["missingIngredients"]) <= minimum_missing + 2]
            row = affordable[0]
        selected.append(row)
        covered.update(canonical(name) for name in row["usesExpiringMainIngredients"])
        pending.remove(next(item for item in pending if (item["source"], item["sourceRecipeId"]) == (row["source"], row["sourceRecipeId"])))
    return selected


# 임박·적합성 우선순위가 같은 후보 안에서만 최근 추천과 동일 주재료 반복을 뒤로 미룬다.
def diversify(rows: list[dict], recent_ids: list[str]) -> list[dict]:
    pending, result, seen_main = list(rows), [], set()
    recent = set(recent_ids)
    while pending:
        priority = min(eligibility_tier(row) for row in pending)
        candidates = [row for row in pending if eligibility_tier(row) == priority]
        def penalty(row: dict) -> tuple[int, int]:
            main = frozenset(canonical(item["name"]) for item in row["_suitability"]["ingredients"] if item["role"] == "MAIN")
            return (int(f"{row['source']}:{row['sourceRecipeId']}" in recent), int(bool(main) and main in seen_main))
        selected = min(candidates, key=penalty)
        selected = dict(selected)
        repeated, duplicate = penalty(selected)
        selected["scoreBreakdown"] = {**selected["scoreBreakdown"], "variety": 0.0 if repeated or duplicate else 1.0}
        if repeated or duplicate:
            selected["reason"] += " 같은 임박·적합성 조건에서 최근 추천 또는 주재료 중복을 고려했습니다."
        result.append(selected)
        seen_main.add(frozenset(canonical(item["name"]) for item in selected["_suitability"]["ingredients"] if item["role"] == "MAIN"))
        pending.remove(next(row for row in pending if row["source"] == selected["source"] and row["sourceRecipeId"] == selected["sourceRecipeId"]))
    return result


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
        reused = bool(cached)
        all_cached = bool(tags and shortlist and len(cached) == len(shortlist))
        if all_cached:
            self.preference_matches.clear()
            self.preference_evidence.clear()
            self.preference_tag_evidence.clear()
            self.rejected_preference_tags.clear()
            self.rejected_preference_identities.clear()
            self.evaluated_identities.clear()
            self.unavailable = False
            selected = shortlist
            self.model = self.expected_model
        else:
            selected = await self._rank(context, shortlist)
        if selected is not None and not all_cached:
            for identity in self.evaluated_identities:
                if identity in keys and identity not in self.rejected_preference_identities and identity not in self.rejected_preference_tags:
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
            async with asyncio.timeout(self.timeout), self.slots:
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
                        result, model = await self.reasoner.rank(payload)
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
            allowed = {"RECIPE_CONTEXT_TOO_LARGE", "RECIPE_MODEL_OR_SCHEMA_MISMATCH", "INVALID_RECIPE_RANK_PERMUTATION", "INVALID_RECIPE_PREFERENCE_IDS"}
            code = str(exc) if type(exc) is ValueError and str(exc) in allowed else "TIME_BUDGET_EXCEEDED" if isinstance(exc, TimeoutError) else "REMOTE_OR_SCHEMA_ERROR"
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code if isinstance(exc, AppError) else None
            logger.warning("recipe_ai_fallback stage=RANK error_type=%s reason_code=%s transient=%s status=%s prompt=%s", type(exc).__name__, code, self.unavailable, status, PROMPT_VERSION)
            self.warnings.append("AI_UNAVAILABLE_OR_INVALID")
            return None
        finally:
            if call_started is not None:
                self.duration_ms += (monotonic() - call_started) * 1000


# 임박 활용 다음에 검증된 취향 일치를 비교하고, 부족 재료에 따른 적합성과 구매 부담은 뒤에 둔다.
def eligibility_tier(row: dict) -> tuple[int, ...]:
    preference = () if row.get("preferenceMatch") in {None, "NOT_REQUESTED"} else (0 if row["preferenceMatch"] == "MATCHED" else 1,)
    if preference:
        preference += (-sum(item["status"] == "MATCHED" for item in row.get("preferenceAssessments", [])),)
    prefix = (*row.get("_expiryPriority", ()), *preference, row["_suitability"]["tier"])
    return (*prefix, len(row["missingIngredients"]), *row.get("_expiryDetail", ())) if "_expiryPriority" in row else prefix


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
            row["_expiryPriority"] = (0 if urgent_main else 1 if urgent else 2,)
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
            row["confirmationReasons"].append("원문 인분이 없어 요청 인분의 필요량을 확정할 수 없습니다.")
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
    # 남은 재료/분량은 FastAPI가 이미 검증했다. 모델이 빠진 중량을 추측할 필요가 없다.
    for count in range(len(shortlist), 0, -1):
        window = shortlist[:count]
        payload = {**base, "candidates": compact_candidates(window, brief=True), "executionBudgetSeconds": 60.0}
        if context.get("preferenceTags"):
            for candidate, row in zip(payload["candidates"], window, strict=True):
                candidate["preferenceEvidenceOptions"] = list(dict.fromkeys(item["name"] for item in row["ingredients"]
                    if len(item["name"]) <= 80 and any(valid_preference_evidence(row, item["name"], [tag]) for tag in context["preferenceTags"])))
        # 실제 HTTP JSON과 같은 공백 없는 직렬화로 검사하고 실행 예산 숫자 변경 여유도 남긴다.
        if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= max_bytes - 64:
            return payload, window
    raise ValueError("RECIPE_CONTEXT_TOO_LARGE")
