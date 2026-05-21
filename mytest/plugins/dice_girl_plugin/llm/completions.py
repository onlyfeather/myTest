from nonebot import logger

from ..config import config
from .client import auth_headers, chat_url


def build_json_payload(model: str, messages: list, temperature: float) -> dict:
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }


async def post_chat_completion(client, payload: dict, log_prefix: str):
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
