"""Tavily로 승인된 원천을 찾고 명시적인 재료·조리 구획만 파싱한다. 조리법은 생성하지 않는다."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import AppError
from app.recipes.ingredients import parse_ingredients
from app.recipes.schemas import Recipe, Step
from app.recipes.tavily import DiscoveryHit, TavilyDiscovery

logger = logging.getLogger(__name__)


@dataclass
class WebSearchResult:
    recipes: list[tuple[Recipe, dict]] = field(default_factory=list)
    searches: int = 0
    unavailable: bool = False


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
def markdown_recipe(content: str) -> dict | None:
    title = re.search(r"(?m)^#\s+([^\n]+)$", content)
    headings = list(re.finditer(r"(?m)^#{1,6}\s+([^\n]+)$", content))
    sections = {}
    for index, heading in enumerate(headings):
        name = heading[1].strip().casefold().rstrip(":")
        if name in {"재료", "준비재료", "ingredients", "조리법", "만드는법", "만드는 법", "조리방법", "조리 방법", "조리순서", "조리 순서", "instructions", "directions"}:
            sections[name] = content[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(content)].strip()
    ingredients = next((sections[key] for key in ("재료", "준비재료", "ingredients") if key in sections), None)
    instructions = next((value for key, value in sections.items() if key not in {"재료", "준비재료", "ingredients"}), None)
    if not title or not ingredients or not instructions:
        return None
    # 구획 안의 목록 표기만 제거하고 조리 문장·수치는 보존한다.
    def lines(text: str) -> list[str]:
        return [re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip() for line in text.splitlines() if line.strip()]
    return {"@type": "Recipe", "name": title[1].strip(), "recipeIngredient": lines(ingredients), "recipeInstructions": lines(instructions)}


# 원문 근거가 충분한 단일 Recipe만 정규화한다. 여러 레시피·요약만 있는 검색 결과는 거부한다.
def parse_web_recipe(hit: DiscoveryHit) -> Recipe | None:
    content = hit.rawContent or ""
    if not content or len(content) > 200000:
        return None
    try:
        documents = re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', content, re.I | re.S)
        nodes = []
        if documents:
            for document in documents[:10]:
                nodes.extend(recipe_nodes(json.loads(document)))
        elif content.lstrip().startswith(("{", "[")):
            nodes = recipe_nodes(json.loads(content))
        else:
            markdown = markdown_recipe(content)
            nodes = [markdown] if markdown else []
        if len(nodes) != 1:
            return None
        row = nodes[0]
        title, raw_ingredients = row.get("name"), row.get("recipeIngredient")
        if not isinstance(title, str) or not isinstance(raw_ingredients, list) or not 1 <= len(raw_ingredients) <= 200 or not all(isinstance(item, str) for item in raw_ingredients):
            return None
        ingredients = parse_ingredients("\n".join(raw_ingredients), title)
        if not ingredients:
            return None
        steps = [Step(stepNo=index, instruction=text) for index, text in enumerate(instruction_texts(row.get("recipeInstructions")), 1)]
        # recipeYield=12가 쿠키 12개인지 12인분인지 추측하지 않는다.
        servings = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:인분|servings?)\s*", str(row.get("recipeYield", "")), re.I)
        return Recipe(source="TAVILY_WEB", sourceRecipeId=hashlib.sha256(hit.url.encode()).hexdigest(),
                      sourceRevision=hashlib.sha256(content.encode()).hexdigest(), sourceUrl=hit.url, name=title,
                      baseServings=float(servings[1]) if servings else None, ingredients=ingredients, steps=steps)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


class WebRecipeProvider:
    # approved_domains는 본문 저장·가공·서비스 제공을 확인한 정확한 호스트 목록이다. 기본값은 비어 있다.
    def __init__(self, settings: Settings, discovery: TavilyDiscovery | None = None) -> None:
        self.settings = settings
        self.discovery = discovery or TavilyDiscovery(settings)

    # 활용되지 않은 임박 재료를 최대 2개 검색한다. 서명 URL·냉장고 ID·재고량은 전달하지 않는다.
    async def search(self, focus: list[str], companions: list[str], timeout_seconds: float = 30) -> WebSearchResult:
        report = WebSearchResult()
        domains = {domain.lower().strip() for domain in self.settings.recipe_tavily_approved_domains}
        if not domains:
            return report
        results = {}
        try:
            async with asyncio.timeout(timeout_seconds):
                for name in list(dict.fromkeys(focus))[:2]:
                    query = " ".join([name, *[value for value in companions if value != name][:2], "레시피 재료 분량 조리법"])
                    report.searches += 1
                    hits = await self.discovery.search(query[:1000], 5, approved_domains=sorted(domains))
                    for hit in hits:
                        if urlsplit(hit.url).hostname not in domains:
                            continue
                        recipe = parse_web_recipe(hit)
                        if recipe:
                            results[(recipe.source, recipe.sourceRecipeId)] = (recipe, {"url": hit.url, "raw_content": hit.rawContent})
        except (AppError, TimeoutError):
            report.unavailable = True
        report.recipes = list(results.values())
        logger.info("recipe_web_source_completed searches=%d validated_count=%d unavailable=%s", report.searches, len(results), report.unavailable)
        return report
