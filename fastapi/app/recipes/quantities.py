"""원본 인분·명시 단위만으로 필요량을 계산하고 소비기한이 가까운 재고부터 가상 배분한다."""

from decimal import Decimal

from app.recipes.retrieval import canonical
from app.recipes.schemas import InventoryItem, Recipe
from app.recipes.suitability import WATER_NAMES


# 레시피 하나를 선택했을 때의 독립 가상 배분이다. 실제 재고 차감이나 여러 요리 동시 배분이 아니다.
def assess_quantities(recipe: Recipe, inventory: list[InventoryItem], servings: int, pantry: list[str]) -> dict:
    pantry_names = {canonical(name) for name in pantry}
    units, available, remaining = {}, {}, {}
    for item in inventory:
        units[item.ingredientId] = "EA" if item.measureType == "COUNT" else item.weightUnit
        available[item.ingredientId] = Decimal(str(item.quantity if item.measureType == "COUNT" else item.weightValue))
        remaining[item.ingredientId] = available[item.ingredientId]
    rows = []
    allocations: dict[int, list[dict]] = {item.ingredientId: [] for item in inventory}
    unresolved: set[int] = set()
    for ingredient in recipe.ingredients:
        matched = sorted([item for item in inventory if canonical(item.name) == canonical(ingredient.name)], key=lambda item: (item.expirationDate, item.ingredientId))
        required = None
        if recipe.baseServings is not None and ingredient.amountValue is not None:
            required = Decimal(str(ingredient.amountValue)) * Decimal(servings) / Decimal(str(recipe.baseServings))
        row = {"ingredientNo": ingredient.ingredientNo, "name": ingredient.name, "amountText": ingredient.amountText,
               "requiredAmount": float(required) if required is not None else None, "unit": ingredient.unit,
               "allocatedAmount": None, "shortageAmount": None, "status": "UNKNOWN_AMOUNT"}
        if required is not None and ingredient.unit is not None:
            usable = [item for item in matched if units[item.ingredientId] == ingredient.unit]
            allocated = Decimal(0)
            for item in usable:
                amount = min(required - allocated, remaining[item.ingredientId])
                remaining[item.ingredientId] -= amount
                allocated += amount
                if amount > 0:
                    allocations[item.ingredientId].append({"ingredientNo": ingredient.ingredientNo, "usedAmount": float(amount)})
            row.update(allocatedAmount=float(allocated), shortageAmount=float(required - allocated))
            row["status"] = "SUFFICIENT" if allocated >= required else "SHORTAGE"
            if not usable and matched:
                row["status"] = "UNIT_MISMATCH"
        # 물은 기본 가용이지만 보유량은 확인하지 않았다. SUFFICIENT로 위조하지 않는다.
        if canonical(ingredient.name) in pantry_names | WATER_NAMES and not matched:
            row.update(status="PANTRY_UNVERIFIED", allocatedAmount=None, shortageAmount=None)
        if row["status"] in {"UNKNOWN_AMOUNT", "UNIT_MISMATCH"}:
            unresolved.update(item.ingredientId for item in matched)
        rows.append(row)
    # 양념/물의 보유량 미확인은 식재료 분량 판정을 막지 않으며 개별 원문 상태는 보존한다.
    food_rows = [row for row in rows if row["status"] != "PANTRY_UNVERIFIED"]
    status = "SHORTAGE" if any(row["status"] == "SHORTAGE" for row in food_rows) else "NEEDS_CHECK"
    if food_rows and all(row["status"] == "SUFFICIENT" for row in food_rows):
        status = "SUFFICIENT"
    usage = [{"ingredientId": item.ingredientId, "name": item.name, "unit": units[item.ingredientId],
              "availableAmount": float(available[item.ingredientId]), "usedAmount": float(available[item.ingredientId] - remaining[item.ingredientId]),
              "remainingAmount": float(remaining[item.ingredientId]), "allocations": allocations[item.ingredientId],
              "allocationStatus": "UNRESOLVED" if item.ingredientId in unresolved else "CALCULATED" if allocations[item.ingredientId] else "UNUSED"} for item in inventory]
    return {"targetServings": servings, "quantityStatus": status, "ingredientRequirements": rows, "inventoryUsage": usage}
