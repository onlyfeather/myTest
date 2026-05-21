import json

from nonebot import logger
from nonebot.adapters import Event

from ..persona import persona_manager
from .client import (
    chat_url,
    get_ai_client,
    has_ai_api_key,
    is_ai_enabled,
    truncate_text,
)
from .completions import build_json_payload, default_model, post_chat_completion
from .prompts import build_dice_prompt, build_dice_user_prompt
from .response import (
    build_offline_dice_reply,
    parse_dice_reaction,
    post_process_reply,
)


async def get_dice_reaction(
    user_fav: int,
    data: dict,
    user_name: str,
    event: Event = None,
) -> dict:
    role = persona_manager.get_persona(event)
    role_name = role.get("meta", {}).get("name", "骰娘")

    sys_prompt = build_dice_prompt(
        role,
        user_fav,
        user_name,
        data["event"],
    )

    user_prompt = build_dice_user_prompt(user_fav, data)

    logger.debug(
        "[Dice] Built AI prompt: event={}, target={}, final_roll={}, status={}",
        data.get("event"),
        data.get("target"),
        data.get("final_roll"),
        data.get("final_status"),
    )

    payload = build_json_payload(
        default_model(),
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.75,
    )

    try:
        if not is_ai_enabled():
            logger.info("[Dice] AI request skipped: AI disabled")
            return {"reply": build_offline_dice_reply(data), "delta": 0}
        if not has_ai_api_key():
            logger.warning("[Dice] AI request skipped: missing DICE_GIRL_AI_API_KEY")
            return {"reply": "(AI 未配置: 缺少 DICE_GIRL_AI_API_KEY)", "delta": 0}

        logger.debug(
            "[Dice] AI request: url={}, model={}, messages={}, prompt_len={}",
            chat_url(),
            default_model(),
            len(payload["messages"]),
            len(sys_prompt) + len(user_prompt),
        )
        async with get_ai_client() as client:
            resp = await post_chat_completion(client, payload, "[Dice]")

            if resp.status_code != 200:
                logger.warning(
                    "[Dice] AI HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                resp.raise_for_status()

            data_resp = resp.json()
            choice = data_resp["choices"][0]
            finish_reason = choice.get("finish_reason", "unknown")
            message = choice.get("message", {})
            content = message.get("content") or ""

            logger.debug(
                "[Dice] Received AI response: finish_reason={}, content_len={}",
                finish_reason,
                len(content),
            )
            if not content:
                logger.warning(
                    "[Dice] AI response content is empty: body={}",
                    truncate_text(json.dumps(data_resp, ensure_ascii=False)),
                )

            result = parse_dice_reaction(content)

            if "reply" in result:
                result["reply"] = post_process_reply(result["reply"], user_name, role_name)

            return result

    except Exception as e:
        logger.exception("[Dice] AI API error: {}: {}", type(e).__name__, str(e))
        return {"reply": "(AI 掉线了，总之就是这个结果)", "delta": 0}
