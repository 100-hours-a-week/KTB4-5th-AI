import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas import Category

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


# 재료 문자열을 NFKC·소문자·공백 제거로 정규화한다. 의미상 동의어를 임의로 생성하지 않는다.
def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InventoryItem(Contract):
    ingredientId: int = Field(gt=0)
    name: Text
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
    limit: int = Field(default=5, ge=1, le=20)

    # 중복 재고 ID와 정규화 재고 fingerprint 일치 여부를 확인한다.
    @model_validator(mode="after")
    def validate_inventory(self) -> "RecommendationRequest":
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


class RecommendedRecipe(RecipeSnapshot):
    score: float = Field(ge=0, le=1)
    scoreBreakdown: ScoreBreakdown
    usesInventory: list[str]
    usesExpiringIngredients: list[str]
    missingIngredients: list[str]
    excludedIngredientCheck: Literal["PASSED"]
    reason: str


class RecommendationResponse(Contract):
    recommendationId: str
    refrigeratorId: int
    inventoryFingerprint: str
    recommendationMode: str
    recommendations: list[RecommendedRecipe]
    generatedAt: datetime


class ChangesResponse(Contract):
    items: list[Recipe]
    nextCursor: str
    hasMore: bool
