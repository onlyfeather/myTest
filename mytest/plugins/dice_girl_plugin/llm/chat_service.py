from nonebot import logger
from nonebot.adapters import Event

from ..persona import persona_manager
from .client import (
    chat_url,
    get_ai_client,
    has_ai_api_key,
    is_ai_enabled,
)
from .completions import build_json_payload, default_model, post_chat_completion
from .prompts import build_chat_prompt
from .response import build_offline_chat_reply, parse_chat_reply, post_process_reply


async def analyze_chat(
    user_fav: int,
    user_text: str,
    user_name: str,
    history: list = None,
    memory_summary: str = "",
    event: Event = None,
) -> dict:
    role = persona_manager.get_persona(event)
    role_name = role.get("meta", {}).get("name", "骰娘")

    sys_prompt = build_chat_prompt(
        role,
        user_fav,
        user_name,
        user_text,
        memory_summary,
    )

    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = build_json_payload(default_model(), messages, temperature=0.9)

    try:
        if not is_ai_enabled():
            logger.info("[AI] Chat request skipped: AI disabled")
            return {"reply": build_offline_chat_reply(user_name), "delta": 0}
        if not has_ai_api_key():
            logger.warning("[AI] Chat request skipped: missing DICE_GIRL_AI_API_KEY")
            return {"reply": "(AI 未配置: 缺少 DICE_GIRL_AI_API_KEY)", "delta": 0}

        logger.debug(
            "[AI] Chat request: url={}, model={}, messages={}, user_text_len={}, history={}",
            chat_url(),
            default_model(),
            len(messages),
            len(user_text),
            len(history or []),
        )
        async with get_ai_client() as client:
            resp = await post_chat_completion(client, payload, "[AI]")

            if resp.status_code != 200:
                logger.warning(
                    "[AI] HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                return {"reply": f"(API 报错: {resp.status_code})", "delta": 0}

            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "unknown")
            content = choice["message"]["content"]

            logger.debug(
                "[AI] Chat response: finish_reason={}, content_len={}",
                finish_reason,
                len(content or ""),
            )

            if finish_reason == "content_filter":
                return {"reply": "*被未知的力量捂住了嘴...* (内容被安全系统拦截)", "delta": 0}

            result = parse_chat_reply(content)

            if "reply" in result:
                result["reply"] = post_process_reply(result["reply"], user_name, role_name)

            return result

    except Exception as e:
        logger.exception(f"[AI] Chat API error: {e}")
        return {"reply": "*捂着头，似乎有些头晕* (连接失败)", "delta": 0}
