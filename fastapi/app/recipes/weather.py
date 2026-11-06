"""기상청 초단기실황 / 초단기예보로 추천 시점의 날씨를 조회하고 기준 시각 단위로 캐싱한다.

현재는 서울 한 격자만 조회한다(사용자 위치 미수집). 날씨 조회가 실패해도 추천은 실패시키지 않는다.
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta
from time import monotonic
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
PRECIPITATION = {"0": "NONE", "1": "RAIN", "2": "SLEET", "3": "SNOW", "5": "RAIN", "6": "SLEET", "7": "SNOW"}
SKY = {"1": "CLEAR", "3": "PARTLY_CLOUDY", "4": "OVERCAST"}


# 실황은 매시 정각 자료가 약 40분 뒤 제공된다. 45분 전에는 직전 시각을 기준으로 한다.
def observation_base(now: datetime) -> datetime:
    local = now.astimezone(KST)
    base = local.replace(minute=0, second=0, microsecond=0)
    return base - timedelta(hours=1) if local.minute < 45 else base


# 기상청 체감온도식. 겨울(10~4월)은 풍속 기반, 여름(5~9월)은 습도 기반 습구온도를 사용한다.
def feels_like(temperature: float, wind_ms: float | None, humidity: float | None, month: int) -> float:
    if 5 <= month <= 9 and humidity is not None:
        wet = (temperature * math.atan(0.151977 * math.sqrt(humidity + 8.313659)) + math.atan(temperature + humidity)
               - math.atan(humidity - 1.67633) + 0.00391838 * humidity ** 1.5 * math.atan(0.023101 * humidity) - 4.686035)
        return round(-0.2442 + 0.55399 * wet + 0.45535 * temperature - 0.0022 * wet ** 2 + 0.00278 * wet * temperature + 3.0, 1)
    speed = (wind_ms or 0) * 3.6
    if temperature <= 10 and speed >= 4.8:
        power = speed ** 0.16
        return round(13.12 + 0.6215 * temperature - 11.37 * power + 0.3965 * temperature * power, 1)
    return round(temperature, 1)


# 모델과 서버 검증이 같은 구간을 쓰도록 체감온도를 다섯 구간으로 나눈다.
def temperature_band(value: float) -> str:
    return "COLD" if value <= 5 else "CHILLY" if value <= 12 else "MILD" if value <= 22 else "WARM" if value <= 27 else "HOT"


# 공공데이터포털은 인코딩 / 디코딩 키를 함께 준다. 어느 쪽을 넣어도 동작하도록 디코딩 값으로 맞춘다.
def service_key(raw: str) -> str:
    return unquote(raw) if "%" in raw else raw


class WeatherService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self._snapshot: dict | None = None
        self._snapshot_base: datetime | None = None
        self._fetched_at = 0.0
        self._failed_base: datetime | None = None
        self._refresh: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.recipe_weather_enabled and bool(self.settings.recipe_kma_api_key)

    # 현재 기준 시각 자료가 있으면 즉시 반환한다. 오래된 자료가 있으면 갱신을 뒤에서 시작하고 기존 값을 쓴다.
    # 자료가 전혀 없을 때만 짧게 기다린다. 실패하면 None이며 추천은 계절 / 시간대만으로 진행한다.
    async def current(self, now: datetime | None = None) -> dict | None:
        if not self.enabled:
            return None
        now = now or datetime.now(KST)
        base = observation_base(now)
        if self._snapshot is not None and self._snapshot_base == base:
            return dict(self._snapshot)
        usable = self._usable_snapshot(now)
        if self._failed_base != base and (self._refresh is None or self._refresh.done()):
            self._refresh = asyncio.create_task(self._fetch(base))
        if usable is not None:
            return usable
        if self._refresh is None:
            return None
        try:
            await asyncio.wait_for(asyncio.shield(self._refresh), self.settings.recipe_weather_wait_seconds)
        except TimeoutError:
            logger.warning("recipe_weather_wait_timeout")
        except Exception:
            pass
        return self._usable_snapshot(now)

    # 허용 시간 안의 이전 자료만 사용하고 오래됐다는 표시를 붙인다.
    def _usable_snapshot(self, now: datetime) -> dict | None:
        if self._snapshot is None or self._snapshot_base is None:
            return None
        age = now.astimezone(KST) - self._snapshot_base
        if age > timedelta(hours=self.settings.recipe_weather_max_stale_hours):
            return None
        return {**self._snapshot, "stale": age > timedelta(hours=2)}

    async def _get(self, operation: str, params: dict) -> list[dict]:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.settings.recipe_weather_timeout_seconds)
        response = await self.client.get(f"{BASE_URL}/{operation}", params={
            "serviceKey": service_key(self.settings.recipe_kma_api_key or ""), "pageNo": 1, "numOfRows": 100, "dataType": "JSON",
            "nx": self.settings.recipe_weather_grid_nx, "ny": self.settings.recipe_weather_grid_ny, **params})
        response.raise_for_status()
        try:
            body = response.json()["response"]
        except (ValueError, KeyError) as exc:
            # 인증 실패 등은 200 응답의 XML 본문으로 올 수 있다. 본문 / 키는 기록하지 않는다.
            raise ValueError("KMA_RESPONSE_NOT_JSON") from exc
        if body["header"]["resultCode"] != "00":
            raise ValueError(f"KMA_RESULT_{body['header']['resultCode']}")
        return body["body"]["items"]["item"]

    # 실황(기온 / 습도 / 풍속 / 강수)과 초단기예보(하늘 상태)를 합친다. 예보 실패는 하늘 상태만 비운다.
    async def _fetch(self, base: datetime) -> None:
        try:
            items = await self._get("getUltraSrtNcst", {"base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H00")})
            values = {item["category"]: item["obsrValue"] for item in items}
            temperature = float(values["T1H"])
            humidity = float(values["REH"]) if "REH" in values else None
            wind = float(values["WSD"]) if "WSD" in values else None
            rainfall = float(values["RN1"]) if str(values.get("RN1", "")).replace(".", "", 1).isdigit() else 0.0
            sky = None
            try:
                forecast_base = base.replace(minute=30)
                forecast = await self._get("getUltraSrtFcst", {"base_date": forecast_base.strftime("%Y%m%d"), "base_time": forecast_base.strftime("%H30")})
                skies = sorted((item["fcstDate"] + item["fcstTime"], item["fcstValue"]) for item in forecast if item["category"] == "SKY")
                sky = SKY.get(skies[0][1]) if skies else None
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                logger.warning("recipe_weather_sky_unavailable")
            felt = feels_like(temperature, wind, humidity, base.month)
            self._snapshot = {"source": "KMA", "region": self.settings.recipe_weather_region, "observedAt": base.isoformat(),
                              "temperatureC": round(temperature, 1), "feelsLikeC": felt, "humidity": humidity, "windSpeedMs": wind,
                              "precipitation": PRECIPITATION.get(str(values.get("PTY", "0")), "NONE"), "rainfallMm1h": rainfall,
                              "sky": sky, "temperatureBand": temperature_band(felt), "stale": False}
            self._snapshot_base, self._fetched_at, self._failed_base = base, monotonic(), None
            logger.info("recipe_weather_refreshed base=%s band=%s precipitation=%s", base.isoformat(), self._snapshot["temperatureBand"], self._snapshot["precipitation"])
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # 같은 기준 시각에서 실패를 반복 호출하지 않는다. 다음 정시 기준에서 다시 시도한다.
            self._failed_base = base
            logger.warning("recipe_weather_failed error_type=%s reason=%s", type(exc).__name__, str(exc) if str(exc).startswith("KMA_") else "-")

    async def close(self) -> None:
        if self._refresh is not None and not self._refresh.done():
            self._refresh.cancel()
            await asyncio.gather(self._refresh, return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()
