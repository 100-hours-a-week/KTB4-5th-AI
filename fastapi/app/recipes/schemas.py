import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas import Category

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Strategy = Literal["USE_EXPIRING", "MINIMIZE_SHOPPING", "SIMPLE_COOKING"]


# 재료 문자열을 NFKC·소문자·공백 제거로 정규화한다. 의미상 동의어를 임의로 생성하지 않는다.
def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InventoryItem(Contract):
    ingredientId: int = Field(gt=0)
    name: Text
    ingredientKey: Text | None = None
    category: Category
    storageType: Literal["REFRIGERATED", "FROZEN"]
    measureType: Literal["COUNT", "WEIGHT"]
    quantity: int | None = Field(default=None, ge=1, le=100, strict=True)
    weightValue: float | None = Field(default=None, gt=0)
    weightUnit: Literal["NONE", "G", "ML"]
    expirationDate: date

    # 재고 단위별 상호 배타적인 수량·중량 조합을 검증하고 자신을 반환한다.
    @model_validator(mode="after")
    def validate_measure(self) -> "InventoryItem":
        if self.measureType == "COUNT":
            valid = self.quantity is not None and self.weightValue is None and self.weightUnit == "NONE"
        else:
            valid = self.quantity is None and self.weightValue is not None and self.weightUnit in {"G", "ML"}
        if not valid or not normalize(self.name):
            raise ValueError("재료명 또는 재고 단위 조합이 올바르지 않습니다.")
        return self


# 재고 ID 순 정렬·이름 정규화·명시적 null을 포함한 JSON UTF-8의 SHA-256을 반환한다.
def inventory_fingerprint(items: list[InventoryItem]) -> str:
    rows = []
    for item in sorted(items, key=lambda item: item.ingredientId):
        row = item.model_dump(mode="json")
        if item.ingredientKey is None:
            row.pop("ingredientKey")
        else:
            row["ingredientKey"] = normalize(item.ingredientKey)
        row["name"] = normalize(item.name)
        rows.append(row)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RecommendationRequest(Contract):
    requestId: Text
    refrigeratorId: int = Field(gt=0)
    inventoryFingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    recommendationMode: Literal["INCLUDE_EXPIRED", "EXCLUDE_EXPIRED"] = "EXCLUDE_EXPIRED"
    inventory: list[InventoryItem] = Field(max_length=300)
    excludedIngredientNames: list[Text] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=10, ge=1, le=10)
    preferenceTags: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] = Field(default_factory=list, max_length=12)
    targetServings: int = Field(default=2, ge=1, le=12, strict=True)
    strategies: list[Strategy] = Field(default_factory=lambda: ["USE_EXPIRING", "MINIMIZE_SHOPPING", "SIMPLE_COOKING"], min_length=1, max_length=3)
    # 냉장고 재고와 분리한, 호출자가 명시적으로 보유를 확인한 기본 양념. 분량은 보증하지 않는다.
    pantryIngredientNames: list[Text] = Field(default_factory=list, max_length=30)
    # 원천 간 ID 충돌을 막기 위해 SOURCE:sourceRecipeId 형식으로 전달한다.
    recentRecipeIds: list[Text] = Field(default_factory=list, max_length=100)

    # 중복 재고 ID와 정규화 재고 fingerprint 일치 여부를 확인한다.
    @model_validator(mode="after")
    def validate_inventory(self) -> "RecommendationRequest":
        self.preferenceTags = list(dict.fromkeys(tag.lstrip("#").strip() for tag in self.preferenceTags))
        if any(not tag for tag in self.preferenceTags):
            raise ValueError("빈 취향 태그는 허용하지 않습니다.")
        if "USE_EXPIRING" not in self.strategies:
            self.strategies = ["USE_EXPIRING", *self.strategies]
        if len(self.strategies) != len(set(self.strategies)):
            raise ValueError("strategies가 중복됩니다.")
        if len({item.ingredientId for item in self.inventory}) != len(self.inventory):
            raise ValueError("ingredientId가 중복됩니다.")
        if self.inventoryFingerprint != inventory_fingerprint(self.inventory):
            raise ValueError("inventoryFingerprint가 재고 내용과 일치하지 않습니다.")
        return self


class Ingredient(Contract):
    ingredientNo: int = Field(gt=0)
    name: Text
    amountText: Text | None = None
    measureType: Literal["COUNT", "WEIGHT", "VOLUME", "UNKNOWN"] = "UNKNOWN"
    amountValue: float | None = Field(default=None, gt=0)
    unit: Literal["EA", "G", "ML"] | None = None

    # 원문을 수치로 확정할 수 없으면 UNKNOWN/null로 유지한다.
    @model_validator(mode="after")
    def validate_amount(self) -> "Ingredient":
        units = {"COUNT": "EA", "WEIGHT": "G", "VOLUME": "ML"}
        if self.measureType == "UNKNOWN":
            valid = self.amountValue is None and self.unit is None
        else:
            valid = self.amountValue is not None and self.unit == units[self.measureType]
            if valid and self.measureType == "COUNT":
                valid = self.amountValue.is_integer()
        if not valid:
            raise ValueError("레시피 재료의 단위 조합이 올바르지 않습니다.")
        return self


class Step(Contract):
    stepNo: int = Field(gt=0)
    instruction: str = Field(min_length=1, max_length=10000)
    imageUrl: str | None = None


class RecipeSnapshot(Contract):
    source: Text
    sourceRecipeId: Text
    sourceRevision: Text
    name: Text
    imageUrl: str | None = None
    sourceUrl: str | None = Field(default=None, max_length=4096, pattern=r"^https?://")
    baseServings: float | None = Field(default=None, gt=0)
    ingredients: list[Ingredient] = Field(min_length=1, max_length=200)
    steps: list[Step] = Field(min_length=1, max_length=100)

    # 자식 번호의 중복·역순을 거부하여 동기화 시 순서를 보존한다.
    @model_validator(mode="after")
    def validate_order(self) -> "RecipeSnapshot":
        for numbers in ([item.ingredientNo for item in self.ingredients], [item.stepNo for item in self.steps]):
            if numbers != sorted(set(numbers)):
                raise ValueError("재료·단계 번호는 중복 없이 오름차순이어야 합니다.")
        return self


class Recipe(RecipeSnapshot):
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"


class ScoreBreakdown(Contract):
    expiringIngredient: float = Field(ge=0, le=1)
    inventoryCoverage: float = Field(ge=0, le=1)
    missingIngredientPenalty: float = Field(ge=0, le=1)
    variety: float = Field(ge=0, le=1)


class IngredientRequirement(Contract):
    ingredientNo: int
    name: str
    amountText: str | None
    requiredAmount: float | None
    unit: Literal["EA", "G", "ML"] | None
    allocatedAmount: float | None
    shortageAmount: float | None
    # PANTRY_UNVERIFIED는 기본 가용 물 또는 명시된 양념의 분량 미확인도 포함한다.
    status: Literal["SUFFICIENT", "SHORTAGE", "UNKNOWN_AMOUNT", "UNIT_MISMATCH", "PANTRY_UNVERIFIED"]


class InventoryUsage(Contract):
    ingredientId: int
    name: str
    unit: Literal["EA", "G", "ML"]
    availableAmount: float
    usedAmount: float
    remainingAmount: float
    allocationStatus: Literal["CALCULATED", "UNRESOLVED", "UNUSED"] = "UNUSED"
    allocations: list["InventoryAllocation"] = Field(default_factory=list)


class InventoryAllocation(Contract):
    ingredientNo: int = Field(gt=0)
    usedAmount: float = Field(ge=0)


class RecommendedRecipe(RecipeSnapshot):
    score: float = Field(ge=0, le=1)
    scoreBreakdown: ScoreBreakdown
    usesInventory: list[str]
    usesExpiringIngredients: list[str]
    missingIngredients: list[str]
    excludedIngredientCheck: Literal["PASSED"]
    reason: str
    strategy: Strategy | None = None
    targetServings: int = 2
    quantityStatus: Literal["SUFFICIENT", "SHORTAGE", "NEEDS_CHECK"] = "NEEDS_CHECK"
    quantityScope: Literal["FOOD_INGREDIENTS"] = "FOOD_INGREDIENTS"
    ingredientRequirements: list[IngredientRequirement] = Field(default_factory=list)
    inventoryUsage: list[InventoryUsage] = Field(default_factory=list)
    essentialIngredientNos: list[int] = Field(default_factory=list)
    missingEssentialIngredientNos: list[int] = Field(default_factory=list)
    feasibility: Literal["NEEDS_SHOPPING", "NEEDS_CHECK", "QUANTITY_VERIFIED"] = "NEEDS_CHECK"
    confirmationReasons: list[str] = Field(default_factory=list)
    usesExpiringMainIngredients: list[str] = Field(default_factory=list)
    preferenceMatch: Literal["MATCHED", "NOT_MATCHED", "UNKNOWN", "NOT_REQUESTED"] = "NOT_REQUESTED"
    preferenceEvidence: str | None = None
    preparationNote: str = "원본 조리법을 확인하세요. 냉동 재료의 해동 조건은 원문·포장 기준을 확인하며 조리·해동시간을 별도 추정하지 않습니다."


class RecommendationTrace(Contract):
    policyVersion: str = "recipe-rerank-v12"
    model: str | None = None
    promptVersion: str | None = None
    aiCalls: int = 0
    searchCount: int = 0
    candidateCount: int = 0
    externalSearchCount: int = 0
    webSearchCount: int = 0
    webDiscoveredCount: int = 0
    webExtractionCalls: int = 0
    webParseFailureCounts: dict[str, int] = Field(default_factory=dict)
    aiCandidateCount: int = 0
    aiInputBytes: int = 0
    timingsMs: dict[str, float] = Field(default_factory=dict)


class MealPlan(Contract):
    focusIngredientIds: list[int] = Field(default_factory=list)
    ideas: list[str] = Field(default_factory=list)


class RecommendationResponse(Contract):
    recommendationId: str
    refrigeratorId: int
    inventoryFingerprint: str
    recommendationMode: str
    recommendations: list[RecommendedRecipe] = Field(max_length=10)
    confirmationCandidates: list[RecommendedRecipe] = Field(default_factory=list, max_length=3)
    uncoveredExpiringIngredients: list[str] = Field(default_factory=list)
    generatedAt: datetime
    recommendationEngine: Literal["AI", "RULE_FALLBACK", "EMPTY"] = "RULE_FALLBACK"
    mealPlan: MealPlan = Field(default_factory=MealPlan)
    warnings: list[str] = Field(default_factory=list)
    trace: RecommendationTrace = Field(default_factory=RecommendationTrace)
    preferenceTags: list[str] = Field(default_factory=list)
    context: dict[str, str] = Field(default_factory=dict)


JobStatus = Literal["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]


class RecipeJobAccepted(Contract):
    jobId: str
    requestId: str
    status: JobStatus
    submittedAt: datetime


class RecipeJobView(RecipeJobAccepted):
    refrigeratorId: int
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    result: RecommendationResponse | None = None
    error: dict | None = None


class ChangesResponse(Contract):
    items: list[Recipe]
    nextCursor: str
    hasMore: bool
