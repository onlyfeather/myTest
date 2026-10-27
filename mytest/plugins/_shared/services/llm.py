"""OpenAI 兼容的对话接口，配置来自 WebUI 的「大模型」分区。"""

from __future__ import annotations

from typing import Any

from ..settings import llm_settings
from . import model_list
from .http import ServiceError, describe_http_error, http_client

NAME = "大模型"


def has_api_key() -> bool:
    return bool(str(llm_settings.api_key).strip())


def chat_url() -> str:
    return f"{str(llm_settings.base_url).rstrip('/')}/chat/completions"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {str(llm_settings.api_key).strip()}"}


def default_model() -> str:
    return str(llm_settings.model)


def extract_content(data: Any) -> str:
    try:
        return str(data["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ServiceError(f"{NAME} 返回格式不对") from None


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    model: str = "",
) -> str:
    """发一次对话请求，返回回复文本。失败时抛 ServiceError。"""
    if not has_api_key():
        raise ServiceError("还没有配置大模型的 API Key")
    payload: dict[str, Any] = {
        "model": model or default_model(),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    async with http_client(llm_settings.proxy, llm_settings.timeout) as client:
        resp = await client.post(chat_url(), json=payload, headers=auth_headers())
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(NAME, resp))
    return extract_content(resp.json())


async def list_models() -> list[tuple[str, str]]:
    ids = await model_list.openai_models(
        NAME, api_key=str(llm_settings.api_key), base_url=str(llm_settings.base_url), proxy=str(llm_settings.proxy)
    )
    return model_list.chat_first(ids)


async def test_connection() -> str:
    reply = await chat([{"role": "user", "content": "只回复两个字：在的"}], temperature=0, max_tokens=10)
    return f"连接正常，模型 {default_model()} 回复：{reply.strip()[:30] or '（空）'}"
