"""검색 결과를 원문 이용 허가와 혼동하지 않고 검토 대기 원천 후보로 다룬다."""

import asyncio
import ipaddress
import json
import logging
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings
from app.errors import DependencyUnavailable

logger = logging.getLogger(__name__)


class DiscoveryHit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=4096)
    searchScore: float | None = Field(default=None, ge=0, le=1)
    status: Literal["PENDING_SOURCE_AND_USAGE_REVIEW"] = "PENDING_SOURCE_AND_USAGE_REVIEW"
    rawContent: str | None = Field(default=None, max_length=200000, repr=False)

    # 외부 공개 HTTP(S) 주소만 후보로 보존한다. 주소를 가져오거나 권한을 승인하지 않는다.
    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError("공개 HTTP(S) URL만 허용합니다.")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("내부 주소는 후보에서 제외합니다.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError("비공개 IP는 후보에서 제외합니다.")
        return value


class TavilyDiscovery:
    # 단일 기본 검색 연결을 준비한다. 비용 중복을 막기 위해 기본 재시도는 0회다.
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    # 레시피/공급자 검색 결과의 URL·제목만 반환한다. 원문·이미지를 수집하거나 Recipe로 승격하지 않는다.
    async def search(self, query: str, max_results: int = 5, approved_domains: list[str] | None = None) -> list[DiscoveryHit]:
        if not self.settings.recipe_tavily_api_key:
            raise DependencyUnavailable("TAVILY_API_KEY가 설정되지 않았습니다.")
        if not query.strip() or len(query) > 1000 or not 1 <= max_results <= 10:
            raise ValueError("검색어 길이는 1~1000, 결과 수는 1~10이어야 합니다.")
        payload = {"query": query, "search_depth": "basic", "max_results": max_results, "include_answer": False,
                   "include_raw_content": False, "include_images": False, "auto_parameters": False, "include_usage": True}
        if approved_domains:
            payload.update(include_domains=approved_domains, include_raw_content=True)
        try:
            async with asyncio.timeout(20), self._lock:
                await asyncio.sleep(max(0, 1 - (time.monotonic() - self._last_request_at)))
                async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=self.transport) as client:
                    for attempt in range(self.settings.recipe_tavily_max_attempts):
                        self._last_request_at = time.monotonic()
                        async with client.stream("POST", "https://api.tavily.com/search", json=payload,
                                                 headers={"Authorization": f"Bearer {self.settings.recipe_tavily_api_key}"}) as response:
                            if response.status_code == 429 and attempt + 1 < self.settings.recipe_tavily_max_attempts:
                                try:
                                    delay = min(2, max(0.1, float(response.headers.get("Retry-After", "1"))))
                                except ValueError:
                                    delay = 1
                                await asyncio.sleep(delay)
                                continue
                            response.raise_for_status()
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 1_000_000:
                                    raise ValueError("검색 응답 크기 초과")
                            body = json.loads(data)
                            break
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise ValueError("검색 응답 형식 오류")
            hits, seen = [], set()
            for row in body["results"][:max_results]:
                if not isinstance(row, dict):
                    continue
                try:
                    hit = DiscoveryHit(title=row.get("title"), url=row.get("url"), searchScore=row.get("score"),
                                       rawContent=row.get("raw_content") if approved_domains else None)
                except ValueError:
                    continue
                if hit.url not in seen:
                    seen.add(hit.url)
                    hits.append(hit)
            usage = body.get("usage", {})
            credits = usage.get("credits") if isinstance(usage, dict) else None
            credits = credits if type(credits) in {int, float} and 0 <= credits <= 1000 else "unknown"
            logger.info("recipe_discovery_completed source=TAVILY count=%d credits=%s", len(hits), credits)
            return hits
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as exc:
            logger.warning("recipe_discovery_failed source=TAVILY error_type=%s", type(exc).__name__)
            raise DependencyUnavailable("Tavily 원천 검색에 실패했습니다.") from None
