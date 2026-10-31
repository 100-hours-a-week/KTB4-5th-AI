"""원문별 맛 평가의 검증 / 버전 키 / 사용자 태그 연결. 실제 관능 평가나 영양 판정은 아니다."""
import hashlib
import json
import re

from app.recipes.ai_contract import PROMPT_VERSION, TasteProfile
from app.recipes.meal_quality import frying_steps, weak_spicy_basis
from app.recipes.retrieval import canonical

EVALUATION_VERSION = "recipe-taste-v1"
KINDS = {"SPICY", "SWEET", "SALTY", "MILD", "PEPPERY"}
TAG_KINDS = {"매운거":"SPICY", "매운맛":"SPICY", "매운음식":"SPICY", "매콤한거":"SPICY",
             "단거":"SWEET", "단맛":"SWEET", "달콤한거":"SWEET", "달달한거":"SWEET",
             "짠거":"SALTY", "짠맛":"SALTY", "짭짤한거":"SALTY", "짭짤한맛":"SALTY",
             "담백한거":"MILD", "담백한맛":"MILD", "담백한음식":"MILD",
             "칼칼한거":"PEPPERY", "칼칼한맛":"PEPPERY"}


# 반례를 거르는 원문 신호이며 맛을 확정하는 분류 규칙이 아니다. 실제 판정에는 전체 원문과 조리 / 분량 근거가 필요하다.
def taste_signal(kind: str, text: str) -> bool:
    value = canonical(text)
    signals = {"SPICY": ("고추", "칠리", "마라", "매운", "매콤"),
               "SWEET": ("설탕", "물엿", "꿀", "시럽", "올리고당", "조청", "단맛", "달콤", "달달"),
               "SALTY": ("소금", "간장", "된장", "고추장", "액젓", "젓갈", "짭짤", "짠맛", "치즈", "베이컨", "햄", "소시지", "김치"),
               "PEPPERY": ("고추", "칠리", "마라", "칼칼")}
    if kind == "MILD":
        return True
    present = any(word in value for word in signals[kind])
    return present


# 사용자 / 재고 / 취향과 독립적으로 원문과 모델 / 평가 버전을 구분한다.
def profile_key(row: dict, model: str) -> str:
    source = {k: row.get(k) for k in ("source", "sourceRecipeId", "sourceRevision", "name", "baseServings", "ingredients", "steps")}
    return hashlib.sha256(json.dumps([source, model, PROMPT_VERSION, EVALUATION_VERSION], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# 분량이 모호하면 숫자로 바꾸지 않는다. 모델에는 전체 원문과 원문 번호만 보낸다.
def profile_context(row: dict) -> dict:
    source = {k: row.get(k) for k in ("name", "baseServings", "ingredients", "steps")}
    options = [{"field":"TITLE", "referenceNo":0, "quote":row["name"][:240]}]
    for item in row["ingredients"]:
        text = item["name"] + " " + (item.get("amountText") or "")
        options.append({"field":"INGREDIENT", "referenceNo":item["ingredientNo"], "quote":text.strip()[:240]})
    for step in row["steps"]:
        text = step["instruction"]
        options.extend({"field":"STEP", "referenceNo":step["stepNo"], "quote":text[start:start+240]}
                       for start in range(0, len(text), 240) if len(text[start:start+240].strip()) >= 2)
    payload = {"recipe": source, "evidenceOptions":options, "evaluationVersion": EVALUATION_VERSION, "executionBudgetSeconds": 30}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
        raise ValueError("TASTE_PROFILE_SOURCE_TOO_LARGE")
    return payload


# 인용은 원문 번호와 본문 모두 일치해야 한다. 잘못된 인용은 전체 응답 오류, 부족한 근거는 차원별 UNKNOWN이다.
def validate_profile(row: dict, profile: TasteProfile) -> TasteProfile:
    if {d.kind for d in profile.dimensions} != KINDS:
        raise ValueError("TASTE_PROFILE_DIMENSIONS_INVALID")
    result = profile.model_copy(deep=True)
    ingredients = {i["ingredientNo"]: i for i in row["ingredients"]}
    steps = {s["stepNo"]: s["instruction"] for s in row["steps"]}
    for dimension in result.dimensions:
        proof = [*dimension.evidence, *([dimension.cookingEvidence] if dimension.cookingEvidence else [])]
        for evidence in proof:
            text = row["name"] if evidence.field == "TITLE" and evidence.referenceNo == 0 else steps.get(evidence.referenceNo) if evidence.field == "STEP" else None
            if evidence.field == "INGREDIENT" and evidence.referenceNo in ingredients:
                item = ingredients[evidence.referenceNo]
                text = item["name"] + " " + (item.get("amountText") or "")
            if not text or re.sub(r"\s+", "", evidence.quote) not in re.sub(r"\s+", "", text):
                raise ValueError("TASTE_PROFILE_EVIDENCE_MISMATCH")
        if dimension.status == "UNKNOWN":
            continue
        step_evidence = any(e.field == "STEP" for e in proof)
        measured = [ingredients[e.referenceNo] for e in proof if e.field == "INGREDIENT"]
        quantified = any(i.get("amountValue") is not None or re.search(r"\d", i.get("amountText") or "") for i in measured)
        complete = all(i.get("amountValue") is not None for i in row["ingredients"])
        if row.get("baseServings") is None:
            dimension.limitations = list(dict.fromkeys(["BASE_SERVINGS_UNAVAILABLE_NO_PORTION_SCALING", *dimension.limitations]))[:8]
        explicit = any(e.field == "TITLE" and any(word in canonical(e.quote) for word in
                       {"SPICY":("매운", "매콤"), "SWEET":("달콤", "달달"), "SALTY":("짭짤",),
                        "MILD":("담백",), "PEPPERY":("칼칼",)}[dimension.kind]) for e in proof)
        # 단일 재료명만으로는 맛을 확정하지 않는다. 조리문과 계량 근거 또는 명시적인 원문 맛 설명을 요구한다.
        supported = step_evidence and (quantified or explicit)
        if dimension.status == "NOT_SUPPORTED":
            supported = step_evidence and complete
            # 해당 맛을 낼 수 있는 원문 재료가 있으면 미지원도 확정하지 않는다.
            if dimension.kind != "MILD" and taste_signal(dimension.kind, " ".join(i["name"] for i in row["ingredients"])):
                supported = False
        if dimension.kind in {"SALTY", "SWEET", "MILD"} and not explicit and not complete:
            supported = False
        if dimension.status == "SUPPORTED" and dimension.kind == "MILD":
            strong = any(any(token in canonical(item["name"]) for token in ("고춧가루", "청양고추", "고추기름", "마라", "칠리", "고추장")) for item in row["ingredients"])
            if frying_steps(row) or strong and not weak_spicy_basis(row):
                supported = False
        if dimension.status == "SUPPORTED" and dimension.kind in {"SPICY", "PEPPERY"} and weak_spicy_basis(row):
            supported = False
        if dimension.status == "SUPPORTED" and not taste_signal(dimension.kind, " ".join(e.quote for e in proof)):
            supported = False
        if not supported:
            dimension.status = "UNKNOWN"
            dimension.limitations = list(dict.fromkeys(["SOURCE_OR_QUANTITY_EVIDENCE_INSUFFICIENT", *dimension.limitations]))[:8]
    return result


# 축약 요청 평가에서 계량 근거가 없는 복합 맛은 미일치를 확정하지 않는다.
def uncertain_unmatched_tags(row: dict, tags: list[str]) -> set[str]:
    if all(item.get("amountValue") is not None for item in row["ingredients"]):
        return set()
    return {tag for tag in tags if TAG_KINDS.get(canonical(tag)) in {"SWEET", "SALTY", "MILD", "PEPPERY"}}


# 공개 응답의 태그 상태로 연결한다. 알 수 없는 사용자 요구는 기존 요청별 평가 대상으로 남긴다.
def preference_decisions(profile: TasteProfile, tags: list[str]) -> dict[str, tuple[str, str | None]]:
    by_kind = {d.kind: d for d in profile.dimensions}
    result = {}
    for tag in tags:
        dimension = by_kind.get(TAG_KINDS.get(canonical(tag)))
        if dimension is None:
            continue
        quote = dimension.cookingEvidence.quote if dimension.cookingEvidence else next((e.quote for e in dimension.evidence if e.field == "STEP"), None)
        result[tag] = ({"SUPPORTED":"MATCHED", "NOT_SUPPORTED":"NOT_MATCHED", "UNKNOWN":"UNKNOWN"}[dimension.status], quote)
    return result
