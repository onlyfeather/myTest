"""OpenAI 兼容的图片生成接口：POST {base}/images/generations，返回 base64。"""

from __future__ import annotations

import base64
from typing import Any

from .http import ServiceError, describe_http_error, http_client

NAME = "GPT Image"

MODELS = (
    ("gpt-image-2.5-flare", "GPT Image 2.5 Flare（快）"),
    ("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst（精细，较慢）"),
    ("gpt-image-2", "GPT Image 2"),
)

QUALITIES = (
    ("auto", "自动"),
    ("low", "低"),
    ("medium", "中"),
    ("high", "高"),
)

SIZES = {
    "竖图": "1024x1536",
    "横图": "1536x1024",
    "方图": "1024x1024",
}


def build_payload(prompt: str, *, model: str, size: str, quality: str, moderation: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size, "quality": quality}
    if moderation:
        payload["moderation"] = moderation
    return payload


async def _fetch_url(url: str, proxy: str, timeout: float) -> bytes:
    async with http_client(proxy, timeout) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise ServiceError(f"{NAME} 图片下载失败：HTTP {resp.status_code}")
    return resp.content


async def extract_image(data: Any, proxy: str, timeout: float) -> bytes:
    try:
        item = data["data"][0]
    except (KeyError, IndexError, TypeError):
        raise ServiceError(f"{NAME} 没有返回图片") from None
    if item.get("b64_json"):
        try:
            return base64.b64decode(item["b64_json"])
        except ValueError:
            raise ServiceError(f"{NAME} 返回的图片无法解码") from None
    if item.get("url"):
        # 一些中转站只返回链接。
        return await _fetch_url(item["url"], proxy, timeout)
    raise ServiceError(f"{NAME} 没有返回图片")


async def generate(
    prompt: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    size: str,
    quality: str = "auto",
    moderation: str = "",
    proxy: str = "",
    timeout: float = 300.0,
) -> bytes:
    if not api_key.strip():
        raise ServiceError("还没有配置 GPT Image 的 API Key")
    payload = build_payload(prompt, model=model, size=size, quality=quality, moderation=moderation)
    url = f"{base_url.rstrip('/')}/images/generations"
    async with http_client(proxy, timeout) as client:
        resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key.strip()}"})
    if resp.status_code != 200:
        message = describe_http_error(NAME, resp)
        if "moderation" in resp.text.lower() or "safety" in resp.text.lower():
            message = f"{NAME} 拒绝了这次请求（内容审核未通过）"
        raise ServiceError(message)
    return await extract_image(resp.json(), proxy, timeout)


async def check_models(*, api_key: str, base_url: str, model: str, proxy: str = "", timeout: float = 30.0) -> str:
    if not api_key.strip():
        raise ServiceError("还没有配置 GPT Image 的 API Key")
    async with http_client(proxy, timeout) as client:
        resp = await client.get(
            f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key.strip()}"}
        )
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(NAME, resp))
    try:
        ids = {str(m.get("id")) for m in resp.json().get("data", [])}
    except (ValueError, AttributeError):
        ids = set()
    if not ids:
        return "密钥可用（接口没有返回模型列表，无法确认模型是否存在）"
    if model in ids:
        return f"密钥可用，模型 {model} 存在"
    return f"密钥可用，但模型列表里没有 {model}，请检查模型名"
