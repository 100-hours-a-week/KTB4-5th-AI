"""웹 원문 파싱과 실패 분류. 검색 / 모델 호출 / 저장은 수행하지 않는다."""

import hashlib
import json
import re

from app.recipes.ingredients import NAME, parse_ingredients, parse_piece
from app.recipes.schemas import Ingredient, Recipe, Step
from app.recipes.tavily import DiscoveryHit
from app.recipes.web_extraction import INGREDIENT_LABELS, STEP_LABELS, mixed_batch, numbered_instructions, section_label, source_servings, source_text, unmeasured_source_ingredient


# schema.org의 Recipe와 HowToSection을 제한된 깊이로 읽고 순서를 유지한다.
def recipe_nodes(value: object, depth: int = 0) -> list[dict]:
    if depth > 8:
        return []
    if isinstance(value, list):
        return [node for child in value[:200] for node in recipe_nodes(child, depth + 1)]
    if isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Recipe" or isinstance(kind, list) and "Recipe" in kind:
            return [value]
        return recipe_nodes(value.get("@graph", []), depth + 1)
    return []


# 원문 조리 절차 외의 사전·문자열을 순서나 분량으로 추측하지 않는다.
def instruction_texts(value: object, depth: int = 0) -> list[str]:
    if depth > 8:
        raise ValueError("조리 구획 깊이 초과")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list) and 1 <= len(value) <= 100:
        return [text for child in value for text in instruction_texts(child, depth + 1)]
    if isinstance(value, dict):
        if value.get("@type") == "HowToSection":
            return instruction_texts(value.get("itemListElement"), depth + 1)
        if value.get("@type") == "HowToStep":
            return instruction_texts(value.get("text"), depth + 1)
    raise ValueError("원문 조리 단계가 불명확합니다.")


# Markdown은 명시적인 제목·재료·조리 구획이 있을 때만 읽는다. 광고·후기는 조리문으로 붙이지 않는다.
def markdown_recipe(content: str, fallback_title: str = "") -> dict | None:
    content = source_text(content)
    lines = content.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ") and line[2:].strip() not in {"블로그", "메뉴"}), fallback_title)
    sections = {}
    active = None
    for line in lines:
        label = section_label(line)
        if label in INGREDIENT_LABELS | STEP_LABELS:
            active = "ingredients" if label in INGREDIENT_LABELS else "steps"
            sections.setdefault(active, [])
        elif re.match(r"^#{1,6}\s", line):
            active = None
        elif active:
            sections[active].append(line)
    ingredients, instructions = sections.get("ingredients"), sections.get("steps")
    if not title or not ingredients or not instructions:
        return None
    # 구획 안의 목록 표기만 제거하고 조리 문장·수치는 보존한다.
    def lines(text: str) -> list[str]:
        return [re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip() for line in text.splitlines() if line.strip()]
    servings = source_servings(content)
    steps = numbered_instructions(instructions)
    if steps == []:
        return None
    return {"@type": "Recipe", "name": title, "recipeIngredient": lines("\n".join(ingredients)), "recipeInstructions": steps or lines("\n".join(instructions)),
            "recipeYield": f"{servings}인분" if servings else None}


# 원문 근거가 충분한 단일 Recipe만 정규화한다. 여러 레시피·요약만 있는 검색 결과는 거부한다.
def parse_web_recipe(hit: DiscoveryHit) -> Recipe | None:
    content = hit.rawContent or ""
    if not content or len(content) > 200000:
        return None
    if mixed_batch(content):
        return None
    try:
        documents = re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', content, re.I | re.S)
        nodes = []
        if documents:
            for document in documents[:10]:
                try:
                    nodes.extend(recipe_nodes(json.loads(document)))
                except ValueError:
                    continue
        elif content.lstrip().startswith(("{", "[")):
            try:
                nodes = recipe_nodes(json.loads(content))
            except ValueError:
                markdown = markdown_recipe(content, hit.title)
                nodes = [markdown] if markdown else []
        else:
            markdown = markdown_recipe(content, hit.title)
            nodes = [markdown] if markdown else []
        if len(nodes) != 1:
            return None
        row = nodes[0]
        title, raw_ingredients = row.get("name"), row.get("recipeIngredient")
        if not isinstance(title, str) or not isinstance(raw_ingredients, list) or not 1 <= len(raw_ingredients) <= 200 or not all(isinstance(item, str) for item in raw_ingredients):
            return None
        ingredients = parse_ingredients("\n".join(raw_ingredients), title)
        if not ingredients:
            ingredients = []
            for text in raw_ingredients:
                parsed = parse_piece(text)
                if parsed:
                    offset = len(ingredients)
                    ingredients.extend(item.model_copy(update={"ingredientNo": offset + index}) for index, item in enumerate(parsed, 1))
                elif NAME.fullmatch(text) and not re.search(r"\d|\s", text) and len(text) <= 30 and not re.search(r"합니다|하세요|넣고|넣어", text):
                    ingredients.append(Ingredient(ingredientNo=len(ingredients) + 1, name=text, amountText=text))
                elif (unmeasured := unmeasured_source_ingredient(text, len(ingredients) + 1)) is not None:
                    ingredients.append(unmeasured)
                else:
                    return None
        steps = [Step(stepNo=index, instruction=text) for index, text in enumerate(instruction_texts(row.get("recipeInstructions")), 1)]
        # recipeYield=12가 쿠키 12개인지 12인분인지 추측하지 않는다.
        servings = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:인분|servings?)\s*", str(row.get("recipeYield", "")), re.I)
        return Recipe(source="TAVILY_WEB", sourceRecipeId=hashlib.sha256(hit.url.encode()).hexdigest(),
                      sourceRevision=hashlib.sha256(content.encode()).hexdigest(), sourceUrl=hit.url, name=title,
                      baseServings=float(servings[1]) if servings else None, ingredients=ingredients, steps=steps)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


# 원문을 로그에 복제하지 않고 규칙 추출 실패 지점만 구분한다. 모델 재시도 가능 여부와 별개다.
def parse_failure_reason(text: str) -> str:
    if not text:
        return "EMPTY_BODY"
    if mixed_batch(text):
        return "MIXED_BATCH"
    labels = {section_label(line) for line in text.splitlines()}
    if labels & INGREDIENT_LABELS and not labels & STEP_LABELS:
        return "STEP_SECTION_NOT_FOUND"
    if not labels & INGREDIENT_LABELS:
        return "INGREDIENT_SECTION_NOT_FOUND"
    return "INGREDIENT_OR_STEP_VALIDATION_FAILED"

