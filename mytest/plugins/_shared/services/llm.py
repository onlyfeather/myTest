"""OpenAI 兼容的对话接口，配置来自 WebUI 的「大模型」分区。"""

from __future__ import annotations

import re
from typing import Any

from nonebot import logger

from ..settings import llm_settings, proxy_for
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


class EmptyReplyError(ServiceError):
    """接口正常返回了，但没有正文。retryable=False 表示原样重试也没用（比如 max_tokens 不够）。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|$)", re.DOTALL | re.IGNORECASE)


def _text_of(content: Any) -> str:
    """content 可能是字符串，也可能是 [{"type": "text", "text": "..."}] 这样的分段列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
        )
    return ""


def extract_content(data: Any) -> str:
    """取出回复正文，去掉推理模型混在正文里的 <think> 段。正文为空时按 finish_reason 说明原因。"""
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ServiceError(f"{NAME} 返回格式不对：{str(data)[:200]}") from None
    text = THINK_BLOCK.sub("", _text_of(message.get("content"))).strip()
    if text:
        return text

    reason = str(choice.get("finish_reason") or "")
    has_reasoning = bool(message.get("reasoning_content") or message.get("reasoning"))
    if reason == "length":
        raise EmptyReplyError(
            f"{NAME} 的输出被截断了，没有正文"
            + ("（模型把 token 都用在思考过程上了，可以在 WebUI「大模型」里调大 max_tokens 或换不思考的模型）"
               if has_reasoning else "（可以在 WebUI「大模型」里调大 max_tokens）"),
            retryable=False,
        )
    if reason == "content_filter":
        raise EmptyReplyError(f"{NAME} 的回复被接口的内容审核拦截了，可以换一个模型或接口")
    if message.get("refusal"):
        raise EmptyReplyError(f"{NAME} 拒绝了这次请求：{str(message['refusal'])[:100]}")
    if has_reasoning:
        raise EmptyReplyError(
            f"{NAME} 只返回了思考过程，没有正文（finish_reason={reason or '无'}），换一个不思考的模型更合适"
        )
    raise EmptyReplyError(f"{NAME} 返回了空内容（finish_reason={reason or '无'}）")


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    model: str = "",
) -> str:
    """发一次对话请求，返回回复文本。失败时抛 ServiceError。max_tokens 不传时用 WebUI 里的设置。"""
    if not has_api_key():
        raise ServiceError("还没有配置大模型的 API Key")
    if max_tokens is None:
        max_tokens = int(llm_settings.max_tokens)
    payload: dict[str, Any] = {
        "model": model or default_model(),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    async with http_client(proxy_for(llm_settings), llm_settings.timeout) as client:
        resp = await client.post(chat_url(), json=payload, headers=auth_headers())
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(NAME, resp))
    try:
        data = resp.json()
    except ValueError:
        raise ServiceError(f"{NAME} 返回的不是 JSON，检查接口地址是否填到了 /v1") from None
    try:
        return extract_content(data)
    except ServiceError:
        # 记下原始返回方便排查；返回体里不含密钥。
        logger.warning("[大模型] 没有拿到正文，model={}，原始返回：{}", payload["model"], str(data)[:800])
        raise


async def list_models() -> list[tuple[str, str]]:
    ids = await model_list.openai_models(
        NAME, api_key=str(llm_settings.api_key), base_url=str(llm_settings.base_url), proxy=proxy_for(llm_settings)
    )
    return model_list.chat_first(ids)


async def test_connection() -> str:
    # 用 WebUI 里的 max_tokens：推理模型的思考过程也算 token，限得太小会只剩空正文。
    reply = await chat([{"role": "user", "content": "只回复两个字：在的"}], temperature=0)
    return f"连接正常，模型 {default_model()} 回复：{reply.strip()[:30] or '（空）'}"
