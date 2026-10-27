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


# 引导式参数：只有写了这些才会追问，否则直接出图。
FLAG_REFERENCE = "参考"  # 要一张参考图（NovelAI 用作角色参考）
FLAG_ARTIST = "画师"  # 询问新的画师串（仅 NovelAI）
FLAG_STYLE = "画风"  # 角色参考时连画风一起参考（仅 NovelAI）
FLAGS = {
    FLAG_REFERENCE: FLAG_REFERENCE,
    "参考图": FLAG_REFERENCE,
    "ref": FLAG_REFERENCE,
    FLAG_ARTIST: FLAG_ARTIST,
    "画师串": FLAG_ARTIST,
    "artist": FLAG_ARTIST,
    FLAG_STYLE: FLAG_STYLE,
    "style": FLAG_STYLE,
}
# 只认独立的「-参数」，不会误伤描述里的连字符（如 x-ray、1-2 个人）。
_FLAG_RE = re.compile(r"(?:^|\s)[-－—](" + "|".join(sorted(map(re.escape, FLAGS), key=len, reverse=True)) + r")(?=\s|$)",
                      re.IGNORECASE)


@dataclass
class DrawRequest:
    size: str
    prompt: str
    flags: frozenset[str] = frozenset()

    @property
    def wants_reference(self) -> bool:
        return FLAG_REFERENCE in self.flags

    @property
    def wants_artist(self) -> bool:
        return FLAG_ARTIST in self.flags

    @property
    def with_style(self) -> bool:
        return FLAG_STYLE in self.flags


def parse_request(text: str) -> DrawRequest:
    """「-参考 横图 一只猫」→ flags={参考}, size=横图, prompt=一只猫。参数可以写在任意位置。"""
    flags = frozenset(FLAGS[m.group(1).lower()] for m in _FLAG_RE.finditer(text))
    text = _FLAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    size = "竖图"
    first, _, rest = text.partition(" ")
    if first in SIZE_WORDS:
        size = SIZE_WORDS[first]
        text = rest.strip()
    return DrawRequest(size=size, prompt=text, flags=flags)


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
    # max_tokens 用「大模型」里的设置：带思考的模型会先把 token 花在思考上，给太少时正文是空的
    reply = await llm.chat(
        [{"role": "system", "content": TRANSLATE_PROMPT}, {"role": "user", "content": prompt}],
        temperature=0.3,
    )
    tags = clean_tags(reply)
    if not tags:
        raise ServiceError(
            f"翻译 tag 失败：大模型（{llm.default_model()}）没有返回正文。"
            "如果是带思考的模型，请在 WebUI「大模型」里换成不思考的模型"
        )
    return tags


def join_prompt(prefix: str, prompt: str) -> str:
    prefix = prefix.strip().rstrip(",").strip()
    return f"{prefix}, {prompt}" if prefix else prompt
