from nonebot import logger

from ..llm.schemas import GroupReply, parse_group_reply
from .client import get_ai_client, has_ai_api_key, is_ai_enabled
from .completions import build_json_payload, default_model, post_chat_completion
from .response import clean_and_parse_json, post_process_reply


async def generate_group_reply(
    prompt: str,
    *,
    must_reply: bool,
    speaker_name: str,
    role_name: str,
) -> GroupReply | None:
    """调用模型生成群聊回复。

    must_reply=True（被 @ / 回复 / 叫名字）时，即使模型说不想回，也给一个兜底回复；
    话题触发时尊重模型的 should_reply。失败返回 None，调用方保持沉默。
    """
    if not is_ai_enabled() or not has_ai_api_key():
        return None

    payload = build_json_payload(
        default_model(),
        [{"role": "system", "content": prompt}],
        temperature=0.9,
    )

    try:
        async with get_ai_client() as client:
            resp = await post_chat_completion(client, payload, "[GroupChat]")
            if resp.status_code != 200:
                logger.warning(
                    "[GroupChat] HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                return None
            choice = resp.json()["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                logger.info("[GroupChat] 回复被内容过滤拦截")
                return None
            content = choice.get("message", {}).get("content") or ""
    except Exception:
        logger.exception("[GroupChat] 调用模型失败")
        return None

    result = parse_group_reply(clean_and_parse_json(content))
    result.replies = [
        post_process_reply(text, speaker_name, role_name) for text in result.replies
    ]

    if must_reply and not result.should_reply:
        if result.replies:
            result.should_reply = True
        else:
            logger.debug("[GroupChat] 被直接呼叫但模型没给回复，保持沉默")
    return result
