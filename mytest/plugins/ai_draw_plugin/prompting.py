"""解析画图指令里的尺寸和描述，并在需要时把中文描述翻译成 NovelAI 的 tag。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .._shared.services import llm
from .._shared.services.http import ServiceError

SIZE_WORDS = {
    "竖图": "竖图",
    "竖": "竖图",
    "横图": "横图",
    "横": "横图",
    "方图": "方图",
    "方": "方图",
}

_CJK = re.compile(r"[\u3400-\u9fff]")

TRANSLATE_PROMPT = """你是 NovelAI / Danbooru 提示词助手。把用户的画面描述改写成英文 Danbooru tag。
要求：
- 只输出 tag，用英文逗号分隔，不要解释，不要换行，不要加引号。
- 人数写在最前面，例如 1girl, solo；然后是外貌、服装、动作、表情、场景、构图、光线。
- 认识的作品角色用 Danbooru 标准写法，例如 hatsune miku, ganyu (genshin impact)。
- 用户已经写好的英文 tag 原样保留。
- 不要自行添加质量词（best quality 之类），也不要添加用户没提到的设定。
- 不超过 60 个 tag。"""


@dataclass
class DrawRequest:
    size: str
    prompt: str


def parse_request(text: str) -> DrawRequest:
    """「横图 一只猫」→ size=横图, prompt=一只猫。"""
    text = text.strip()
    size = "竖图"
    first, _, rest = text.partition(" ")
    if first in SIZE_WORDS:
        size = SIZE_WORDS[first]
        text = rest.strip()
    return DrawRequest(size=size, prompt=text)


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text))


def clean_tags(text: str) -> str:
    text = text.strip().strip("`").strip()
    text = re.sub(r"^(tags?|prompt)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("，", ",").replace("\n", ", ")
    tags = [t.strip().strip("\"'") for t in text.split(",")]
    seen: set[str] = set()
    result = []
    for tag in tags:
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return ", ".join(result)


async def to_novelai_tags(prompt: str) -> str:
    reply = await llm.chat(
        [{"role": "system", "content": TRANSLATE_PROMPT}, {"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=400,
    )
    tags = clean_tags(reply)
    if not tags:
        raise ServiceError("翻译 tag 失败：大模型返回为空")
    return tags


def join_prompt(prefix: str, prompt: str) -> str:
    prefix = prefix.strip().rstrip(",").strip()
    return f"{prefix}, {prompt}" if prefix else prompt
