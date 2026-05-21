from nonebot import logger

from ..config import config
from .client import auth_headers, chat_url


def build_json_payload(model: str, messages: list, temperature: float) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if config.ai_json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _messages_char_count(messages: list) -> int:
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += len(str(message.get("content", "")))
        else:
            total += len(str(message))
    return total


async def post_chat_completion(client, payload: dict, log_prefix: str):
    messages = payload.get("messages") or []
    logger.info(
        "{} request payload: model={}, messages={}, chars={}, json_mode={}",
        log_prefix,
        payload.get("model"),
        len(messages),
        _messages_char_count(messages),
        "response_format" in payload,
    )

    resp = await client.post(
        chat_url(),
        json=payload,
        headers=auth_headers(),
    )

    if resp.status_code not in {400, 422} or "response_format" not in payload:
        return resp

    logger.warning(
        "{} JSON response_format rejected: status={}, body={}",
        log_prefix,
        resp.status_code,
        resp.text[:500],
    )
    fallback_payload = dict(payload)
    fallback_payload.pop("response_format", None)
    return await client.post(
        chat_url(),
        json=fallback_payload,
        headers=auth_headers(),
    )


def default_model() -> str:
    return config.ai_model
