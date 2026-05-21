from nonebot import logger

from .client import get_ai_client, has_ai_api_key, is_ai_enabled
from .completions import build_json_payload, default_model, post_chat_completion
from .response import clean_and_parse_json


def build_summary_prompt(old_summary: str, messages: list[dict]) -> str:
    lines = []
    for message in messages:
        role = "用户" if message.get("role") == "user" else "角色"
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")

    transcript = "\n".join(lines) or "无新增内容。"
    old_summary = old_summary.strip() or "暂无。"

    return f"""
你是长期记忆摘要器。请把旧摘要和新增对话合并成稳定、简洁的中文长期记忆。

<old_summary>
{old_summary}
</old_summary>

<new_dialogue>
{transcript}
</new_dialogue>

<rules>
- 只保留对后续角色扮演有用的信息：用户偏好、关系进展、重要承诺、反复出现的话题、角色对用户形成的印象。
- 不要记录无意义寒暄、一次性语气词、骰点数字流水账。
- 不要把角色的临时动作误写成长期事实。
- 不要超过 500 个中文字符。
- 只返回合法 JSON object。
</rules>

<output_schema>
{{"summary":"合并后的长期记忆摘要"}}
</output_schema>
""".strip()


async def summarize_memory(old_summary: str, messages: list[dict]) -> str | None:
    if not messages:
        return old_summary.strip() or None
    if not is_ai_enabled() or not has_ai_api_key():
        return None

    prompt = build_summary_prompt(old_summary, messages)
    payload = build_json_payload(
        default_model(),
        [{"role": "system", "content": prompt}],
        temperature=0.2,
    )

    try:
        async with get_ai_client() as client:
            resp = await post_chat_completion(client, payload, "[MemorySummary]")
            if resp.status_code != 200:
                logger.warning(
                    "[MemorySummary] HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                return None

            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            parsed = clean_and_parse_json(content)
            summary = str(parsed.get("summary") or "").strip()
            return summary or None
    except Exception as e:
        logger.warning("[MemorySummary] Failed: {}: {}", type(e).__name__, str(e))
        return None
