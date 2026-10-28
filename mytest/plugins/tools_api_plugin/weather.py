"""凛音所在城市的实时天气，给说说当背景。用 Open-Meteo，免费、不用 API Key。

查不到时返回空字符串，说说照常写，只是不带天气。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from nonebot import logger

from .._shared.services.http import (
    ServiceError,
    describe_exception,
    describe_http_error,
    http_client,
)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 30 * 60
TIMEOUT = 10.0
FEELS_GAP = 2  # 体感和气温差这么多度才提
WINDY = 30  # km/h

# WMO 天气代码
CODES = {
    0: "晴", 1: "晴，有少量云", 2: "多云", 3: "阴天", 45: "有雾", 48: "有雾，结霜",
    51: "下毛毛雨", 53: "下毛毛雨", 55: "下毛毛雨", 56: "下冻毛毛雨", 57: "下冻毛毛雨",
    61: "下小雨", 63: "下中雨", 65: "下大雨", 66: "下冻雨", 67: "下冻雨",
    71: "下小雪", 73: "下中雪", 75: "下大雪", 77: "下米雪",
    80: "下阵雨", 81: "下阵雨", 82: "下暴雨", 85: "下阵雪", 86: "下大阵雪",
    95: "打雷下雨", 96: "打雷下雨，还有冰雹", 99: "打雷下雨，还有冰雹",
}

_places: dict[str, tuple[float, float, str]] = {}
_cache: dict[str, tuple[float, str]] = {}


async def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    async with http_client(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
    if resp.is_error:
        raise ServiceError(describe_http_error("Open-Meteo", resp))
    data = resp.json()
    if not isinstance(data, dict):
        raise ServiceError("Open-Meteo 返回格式不对")
    return data


async def locate(city: str) -> tuple[float, float, str]:
    """城市名 → (纬度, 经度, 显示名)。「杭州市」这种带「市」的查不到，去掉再查。"""
    key = city.strip()
    if key in _places:
        return _places[key]
    for name in dict.fromkeys((key, key.removesuffix("市"))):
        data = await _get_json(GEOCODING_URL, {"name": name, "count": 1, "language": "zh", "format": "json"})
        if results := data.get("results"):
            place = results[0]
            found = (float(place["latitude"]), float(place["longitude"]), str(place.get("name") or name))
            _places[key] = found
            return found
    raise ServiceError(f"找不到「{city}」这个城市，换个写法试试（比如「杭州」）")


def describe(name: str, data: dict[str, Any]) -> str:
    """把 Open-Meteo 的结果写成一句话，例如「杭州现在下小雨，16°C，今天 15 到 22°C，降水概率 75%」。"""
    current = data.get("current") or {}
    daily = data.get("daily") or {}

    def first(key: str) -> Any:
        values = daily.get(key) or []
        return values[0] if values else None

    sky = CODES.get(int(current.get("weather_code", -1)), "")
    parts = [f"{name}现在{sky}" if sky else f"{name}现在"]
    if (temp := current.get("temperature_2m")) is not None:
        text = f"{round(temp)}°C"
        feels = current.get("apparent_temperature")
        if feels is not None and abs(feels - temp) >= FEELS_GAP:
            text += f"（体感 {round(feels)}°C）"
        parts.append(text)
    low, high = first("temperature_2m_min"), first("temperature_2m_max")
    if low is not None and high is not None:
        parts.append(f"今天 {round(low)} 到 {round(high)}°C")
    if (rain := first("precipitation_probability_max")) is not None:
        parts.append(f"降水概率 {round(rain)}%")
    if (wind := current.get("wind_speed_10m")) is not None and wind >= WINDY:
        parts.append("风很大")
    return "，".join(parts)


async def fetch(city: str) -> str:
    """查天气，出错时抛异常。结果缓存半小时。"""
    key = city.strip()
    if (cached := _cache.get(key)) and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    lat, lon, name = await locate(key)
    data = await _get_json(FORECAST_URL, {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Shanghai",
        "forecast_days": 1,
    })
    text = describe(name, data)
    _cache[key] = (time.monotonic(), text)
    return text


async def current_weather(city: str) -> str:
    """查天气给说说用；没填城市或查不到时返回空字符串，不影响发说说。"""
    if not city.strip():
        return ""
    try:
        return await fetch(city)
    except (ServiceError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("[说说] 查天气失败，这次不带天气：{}", describe_exception("天气", e))
        return ""
