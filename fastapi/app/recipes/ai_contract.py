"""FastAPI와 GPU Worker 사이의 제한된 레시피 계획·선택 계약. 두 배포 경계에서 동일하게 유지한다."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PROMPT_VERSION = "recipe-grounded-v11"
# 저장 / 캐시 키에 쓰는 단계별 프롬프트 버전이다. 해당 단계 프롬프트가 바뀔 때만 올려 기존 평가 / 추출 결과를 보존한다.
PROFILE_PROMPT_VERSION = "recipe-grounded-v10"
EXTRACT_PROMPT_VERSION = "recipe-grounded-v10"
ATTRIBUTE_PROMPT_VERSION = "recipe-attribute-v1"
Strategy = Literal["USE_EXPIRING", "MINIMIZE_SHOPPING", "SIMPLE_COOKING"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanResult(WireModel):
    focusIngredientIds: list[int] = Field(min_length=1, max_length=12)
    ideas: list[str] = Field(min_length=1, max_length=3)


class Selection(WireModel):
    candidateId: int = Field(ge=1)
    strategy: Strategy
    essentialIngredientNos: list[int] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=500)


class SelectionResult(WireModel):
    selections: list[Selection] = Field(max_length=20)


class PreferenceEvidence(WireModel):
    candidateId: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)


# 모델이 고른 추천 이유 코드다. 서버가 날씨 / 시간대 / 저장 속성과 대조해 맞는 코드만 사용자 문구로 바꾼다.
ContextFactor = Literal["WARM_FOR_COLD", "SOUP_FOR_RAIN", "LIGHT_FOR_HEAT", "MEAL_TIME_FIT", "SEASON_FIT",
                        "PREFERENCE_FIT", "USES_EXPIRING", "LOW_SHOPPING"]


class ContextReason(WireModel):
    candidateId: int = Field(ge=1)
    factors: list[ContextFactor] = Field(min_length=1, max_length=4)


class RankResult(WireModel):
    candidateIds: list[int] = Field(min_length=1, max_length=20)
    preferenceMatchedCandidateIds: list[int] = Field(default_factory=list, max_length=20)
    preferenceEvidence: list[PreferenceEvidence] = Field(default_factory=list, max_length=60)
    contextReasons: list[ContextReason] = Field(default_factory=list, max_length=20)


class ExtractedIngredient(WireModel):
    name: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=1000)


class ExtractedRecipe(WireModel):
    title: str = Field(min_length=1, max_length=500)
    ingredients: list[ExtractedIngredient] = Field(max_length=80)
    steps: list[str] = Field(max_length=60)
    status: Literal["EXTRACTED", "INSUFFICIENT", "MIXED_BATCH"]


TasteKind = Literal["SPICY", "SWEET", "SALTY", "MILD", "PEPPERY"]


class TasteEvidence(WireModel):
    field: Literal["TITLE", "INGREDIENT", "STEP"]
    referenceNo: int = Field(ge=0, strict=True)
    quote: str = Field(min_length=2, max_length=240)


class TasteDimension(WireModel):
    kind: TasteKind
    status: Literal["SUPPORTED", "NOT_SUPPORTED", "UNKNOWN"]
    evidence: list[TasteEvidence] = Field(max_length=4)
    cookingEvidence: TasteEvidence | None = None
    limitations: list[str] = Field(default_factory=list, max_length=8)


class TasteProfile(WireModel):
    # 맛 강도 / 확률 / 영양값을 생성하지 않는다. UNKNOWN은 평가 실패와 구분한다.
    dimensions: list[TasteDimension] = Field(min_length=5, max_length=5)


class RecipeAttributes(WireModel):
    # 원천 레시피의 요리 성격 추정이다. 사용자 / 재고 / 날씨와 무관하며 판단이 어려우면 UNKNOWN 또는 빈 목록이다.
    cuisine: Literal["KOREAN", "WESTERN", "CHINESE", "JAPANESE", "OTHER", "UNKNOWN"]
    dishForm: Literal["SOUP_STEW", "RICE", "NOODLE", "SIDE_DISH", "MAIN_DISH", "SALAD", "BAKED", "DESSERT", "DRINK", "OTHER", "UNKNOWN"]
    servingTemperature: Literal["HOT", "COLD", "ROOM", "UNKNOWN"]
    heaviness: Literal["LIGHT", "MEDIUM", "HEAVY", "UNKNOWN"]
    difficulty: Literal["EASY", "MEDIUM", "HARD", "UNKNOWN"]
    mealPurposes: list[Literal["MEAL", "QUICK_MEAL", "SNACK", "DESSERT"]] = Field(max_length=4)
    textures: list[Literal["CRISPY", "CHEWY", "SOFT"]] = Field(max_length=3)
    limitations: list[Annotated[str, StringConstraints(max_length=80)]] = Field(default_factory=list, max_length=6)


Stage = Literal["PLAN", "SELECT", "RANK", "EXTRACT", "PROFILE", "ATTRIBUTE"]


class RecipeAiRequest(WireModel):
    stage: Stage
    # 값 내부도 FastAPI에서 만든 재고·원천 스냅샷이다. Worker는 도구 실행·DB 접근을 하지 않는다.
    context: dict


class RecipeAiResponse(WireModel):
    stage: Stage
    result: PlanResult | SelectionResult | RankResult | ExtractedRecipe | TasteProfile | RecipeAttributes
    model: str
    promptVersion: str = PROMPT_VERSION
    # Worker의 모델 사용량(토큰 수 / 프롬프트 처리 / 생성 / 로드 시간). 운영 진단용이며 구버전 Worker는 보내지 않는다.
    usage: dict[str, float | int | None] | None = None
