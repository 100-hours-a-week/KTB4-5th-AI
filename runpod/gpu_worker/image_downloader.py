import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.adapters.image_loader import ConfiguredImageLoader
from app.config import Settings
from app.domain.models import LoadedImage
from app.errors import DependencyUnavailable, InvalidRequest


class SecureImageDownloader:
    # 허용 호스트·사설 주소 정책과 다운로드 제한이 포함된 이미지 URL 로더를 구성한다.
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._allowed_hosts = tuple(
            host.strip().lower()
            for host in settings.worker_allowed_image_hosts.split(",")
            if host.strip()
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.worker_download_timeout_seconds,
            follow_redirects=False,
        )
        self._validator = ConfiguredImageLoader(settings)

    # URL의 스킴·호스트·주소를 검증하고 제한 크기까지만 받아 이미지와 해시를 검증한다.
    async def load(self, image_url: str, expected_sha256: str) -> LoadedImage:
        await self._validate_url(image_url)
        try:
            async with self._client.stream("GET", image_url) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._settings.max_image_bytes:
                        break
        except httpx.HTTPError as exc:
            raise DependencyUnavailable("원격 이미지 URL을 내려받지 못했습니다.") from exc
        return self._validator.validate_image(bytes(content), expected_sha256)

    # URL이 HTTPS 허용 호스트인지 확인하고 운영 모드에서는 사설·루프백 주소를 차단한다.
    async def _validate_url(self, image_url: str) -> None:
        parsed = urlparse(image_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ({"http", "https"} if self._settings.worker_allow_private_image_urls else {"https"}):
            raise InvalidRequest("허용되지 않은 이미지 URL 스킴입니다.")
        if not hostname or parsed.username or parsed.password:
            raise InvalidRequest("허용되지 않은 이미지 URL입니다.")
        if self._allowed_hosts and not any(
            hostname == allowed or (allowed.startswith(".") and hostname.endswith(allowed))
            for allowed in self._allowed_hosts
        ):
            raise InvalidRequest("허용되지 않은 이미지 URL 호스트입니다.")
        if self._settings.worker_allow_private_image_urls:
            return
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, parsed.port or 443)
        except OSError as exc:
            raise InvalidRequest("이미지 URL 호스트를 확인하지 못했습니다.") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise InvalidRequest("사설 또는 로컬 이미지 주소는 사용할 수 없습니다.")

    # 이 다운로더가 생성한 HTTP 연결만 종료한다.
    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
