"""Tavily 전체 웹 검색에서 명시적인 재료/조리 구획을 검증한다. 원천 DB 저장 승인은 별도로 판단한다."""

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from time import monotonic

import httpx

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings
from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.ingredients import NAME, parse_ingredients, parse_piece
from app.recipes.preferences import explicit_preference_signal, search_preference_terms
from app.recipes.retrieval import canonical
from app.recipes.schemas import Ingredient, Recipe, Step
from app.recipes.tavily import DiscoveryHit, TavilyDiscovery
from app.recipes.web_extraction import INGREDIENT_LABELS, STEP_LABELS, mixed_batch, numbered_instructions, section_label, source_servings, source_text, unmeasured_source_ingredient, validate_extraction

logger = logging.getLogger(__name__)


@dataclass
class WebSearchResult:
    recipes: list[tuple[Recipe, dict]] = field(default_factory=list)
    searches: int = 0
    unavailable: bool = False
    discovered: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    extraction_calls: int = 0
    extraction_ms: float = 0
    search_cache_hits: int = 0
    extraction_cache_hits: int = 0


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


class WebRecipeProvider:
    # 전체 웹 검색과 원문 구획 검증을 담당한다. 원천 DB 등록 승인은 서비스 계층에서 별도로 확인한다.
    def __init__(self, settings: Settings, discovery: TavilyDiscovery | None = None, reasoner: RecipeReasoner | None = None) -> None:
        self.settings = settings
        self.discovery = discovery or TavilyDiscovery(settings)
        self.reasoner = reasoner
        self._search_cache = BoundedAsyncCache[list[DiscoveryHit]](16, min(settings.recipe_cache_ttl_seconds, 120))
        self._parsed_cache: OrderedDict[str, tuple[float, Recipe]] = OrderedDict()

    # 활용되지 않은 임박 재료를 최대 2개 검색한다. 서명 URL·냉장고 ID·재고량은 전달하지 않는다.
    async def search(self, focus: list[str], companions: list[str], timeout_seconds: float = 30, preference_tags: list[str] | None = None) -> WebSearchResult:
        report = WebSearchResult()
        results = {}
        try:
            async with asyncio.timeout(timeout_seconds):
                for name in list(dict.fromkeys(focus))[:2]:
                    # 재고 전체를 검색 필수 조건처럼 묶지 않는다. 취향 검색은 핵심 임박 재료에 집중한다.
                    companion_terms = [] if preference_tags else [value for value in companions if value != name][:2]
                    query = " ".join([name, *companion_terms, *search_preference_terms(preference_tags or [])[:4], "레시피 재료 분량 조리법"])
                    searched = False
                    async def search_hits(search_query: str = query[:1000]) -> list[DiscoveryHit]:
                        nonlocal searched
                        searched = True
                        report.searches += 1
                        return await self.discovery.search(search_query, 5, fetch_raw_content=True)
                    hits = await self._search_cache.get_or_load(query[:1000], search_hits)
                    if not searched:
                        report.search_cache_hits += 1
                    if not any(hit.rawContent for hit in hits):
                        self._search_cache.forget(query[:1000])
                    report.discovered += len(hits)
                    for hit in hits:
                        clean = source_text(hit.rawContent or "")
                        key = hashlib.sha256(json.dumps([hit.url, hit.title, clean, self.settings.ollama_model, PROMPT_VERSION], ensure_ascii=False).encode()).hexdigest()
                        cached = self._parsed_cache.get(key)
                        if cached and monotonic() - cached[0] < self.settings.recipe_cache_ttl_seconds:
                            recipe = cached[1].model_copy(deep=True)
                            recipe.sourceRevision = hashlib.sha256((hit.rawContent or "").encode()).hexdigest()
                            self._parsed_cache.move_to_end(key)
                            report.extraction_cache_hits += 1
                        else:
                            self._parsed_cache.pop(key, None)
                            recipe = parse_web_recipe(hit)
                        failure = None
                        if not recipe:
                            text = source_text(hit.rawContent or "")
                            failure = parse_failure_reason(text)
                            if failure not in {"MIXED_BATCH", "EMPTY_BODY"} and self.reasoner and self.settings.recipe_ai_enabled and report.extraction_calls < 2:
                                if len(text.encode()) > 24000:
                                    failure = "EXTRACTION_INPUT_TOO_LARGE"
                                else:
                                    report.extraction_calls += 1
                                    extraction_started = monotonic()
                                    try:
                                        async with asyncio.timeout(16):
                                            extracted = await self.reasoner.extract({"sourceTitle": hit.title, "sourceText": text, "executionBudgetSeconds": 15})
                                        verified = validate_extraction(extracted, text, hit.title)
                                        if verified:
                                            recipe = Recipe(source="TAVILY_WEB", sourceRecipeId=hashlib.sha256(hit.url.encode()).hexdigest(),
                                                            sourceRevision=hashlib.sha256((hit.rawContent or "").encode()).hexdigest(), sourceUrl=hit.url,
                                                            name=verified["name"], baseServings=source_servings(text), ingredients=verified["ingredients"],
                                                            steps=[Step(stepNo=index, instruction=value) for index, value in enumerate(verified["steps"], 1)])
                                        else:
                                            failure = "MIXED_BATCH" if extracted.status == "MIXED_BATCH" else "MODEL_SOURCE_EVIDENCE_REJECTED"
                                    except (AppError, httpx.HTTPError, ValueError, TypeError, TimeoutError):
                                        failure = "MODEL_EXTRACTION_UNAVAILABLE"
                                        report.unavailable = True
                                    finally:
                                        report.extraction_ms += (monotonic() - extraction_started) * 1000
                        if recipe:
                            self._parsed_cache[key] = (monotonic(), recipe.model_copy(deep=True))
                            self._parsed_cache.move_to_end(key)
                            while len(self._parsed_cache) > 64:
                                self._parsed_cache.popitem(last=False)
                            results[(recipe.source, recipe.sourceRecipeId)] = (recipe, {"url": hit.url, "raw_content": hit.rawContent})
                            # 이미 이 재료를 쓰는 취향 후보를 확보했으면 다른 페이지 추출은 생략한다.
                            relevant = any(canonical(item.name) == canonical(name) for item in recipe.ingredients)
                            if relevant and (not preference_tags or explicit_preference_signal(recipe.model_dump(), preference_tags)):
                                break
                        elif failure:
                            report.failure_reasons[failure] = report.failure_reasons.get(failure, 0) + 1
                            logger.info("recipe_web_parse_rejected reason=%s", failure)
        except (AppError, TimeoutError):
            report.unavailable = True
            report.failure_reasons["SEARCH_OR_EXTRACTION_UNAVAILABLE"] = report.failure_reasons.get("SEARCH_OR_EXTRACTION_UNAVAILABLE", 0) + 1
        report.recipes = list(results.values())
        logger.info("recipe_web_source_completed searches=%d discovered_count=%d validated_count=%d unavailable=%s", report.searches, report.discovered, len(results), report.unavailable)
        return report
