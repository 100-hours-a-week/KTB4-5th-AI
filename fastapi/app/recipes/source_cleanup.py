"""검토한 한식 정밀레시피의 안내와 조리 단계를 분리한다. 조리문은 생성하지 않는다."""

import hashlib
import json
import re

from app.recipes.schemas import Recipe, Step

SOURCE_URL = "https://www.data.go.kr/data/15142529/fileData.do"
PHASE = re.compile(r"\[(?:준비하기|조리하기|담기)\]")


def clean_precision_recipe(recipe: Recipe) -> Recipe:
    if recipe.source != "HANSIK_PRECISION":
        return recipe
    attribution = recipe.sourceAttribution
    notes = list(recipe.cookingNotes)
    steps = []
    for step in recipe.steps:
        text = step.instruction
        match = PHASE.search(text)
        prefix = text[:match.start()] if match else text
        recognized = []
        for line in prefix.splitlines():
            line = line.strip()
            if line.startswith("출처: 한식진흥원"):
                attribution = line
                recognized.append(line)
            elif line.startswith("가. 인덕션 불의 강도에 따른 용어 정리"):
                if line not in notes:
                    notes.append(line)
                recognized.append(line)
        # 확인한 안내문만 제거한다. 알 수 없는 접두문이나 실제 단계는 보존한다.
        if recognized:
            text = "\n".join(line for line in prefix.splitlines() if line.strip() not in recognized).strip()
            if match:
                text = (text + "\n" + step.instruction[match.start():]).strip()
        if text:
            steps.append(Step(stepNo=len(steps) + 1, instruction=text, imageUrl=step.imageUrl))
    if not steps:
        raise ValueError("한식 정밀레시피의 실제 조리 단계가 없습니다.")
    updated = recipe.model_copy(update={"steps": steps, "sourceAttribution": attribution,
                                        "cookingNotes": notes, "sourceUrl": recipe.sourceUrl or SOURCE_URL})
    if updated == recipe:
        return recipe
    payload = updated.model_dump(mode="json", exclude={"sourceRevision"})
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return updated.model_copy(update={"sourceRevision": revision})
