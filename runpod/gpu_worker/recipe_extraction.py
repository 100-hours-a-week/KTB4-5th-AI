"""모델은 조리문을 재작성하지 않고 원문 행 범위를 선택한다. 공개 EXTRACT 결과는 기존 원문 형식을 유지한다."""
from typing import Literal

from pydantic import Field

from gpu_worker.recipe_contract import ExtractedIngredient, ExtractedRecipe, WireModel


class SourceRange(WireModel):
    firstLine: int = Field(ge=1, strict=True)
    lastLine: int = Field(ge=1, strict=True)


class SourceSelection(WireModel):
    title: str = Field(min_length=1, max_length=500)
    ingredients: list[ExtractedIngredient] = Field(max_length=80)
    stepRanges: list[SourceRange] = Field(max_length=60)
    status: Literal["EXTRACTED", "INSUFFICIENT", "MIXED_BATCH"]


# 줄바꿈을 포함한 전체 원문에 번호를 붙인다. 원문을 자르거나 요약하지 않는다.
def source_lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if not 1 <= len(lines) <= 2000:
        raise ValueError("RECIPE_SOURCE_LINE_LIMIT_EXCEEDED")
    return lines


# 선택 범위는 증가 / 비중복이어야 하며 조리문과 숫자를 그대로 복원한다.
def restore_selection(selection: SourceSelection, text: str) -> ExtractedRecipe:
    lines = source_lines(text)
    steps, previous = [], 0
    for span in selection.stepRanges:
        if not previous < span.firstLine <= span.lastLine <= len(lines):
            raise ValueError("RECIPE_EXTRACT_EVIDENCE_MISMATCH")
        step = "".join(lines[span.firstLine-1:span.lastLine]).rstrip("\r\n")
        if not step.strip() or len(step) > 10000:
            raise ValueError("RECIPE_EXTRACT_EVIDENCE_MISMATCH")
        steps.append(step)
        previous = span.lastLine
    if selection.status != "EXTRACTED" and (selection.ingredients or steps):
        raise ValueError("RECIPE_EXTRACT_EVIDENCE_MISMATCH")
    return ExtractedRecipe(title=selection.title, ingredients=selection.ingredients, steps=steps, status=selection.status)
