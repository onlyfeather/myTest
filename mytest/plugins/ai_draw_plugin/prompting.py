"""解析画图指令里的尺寸和描述，并在需要时把中文描述翻译成 NovelAI 的 tag。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from nonebot import logger

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
输出格式：只输出一个 JSON 对象，形如 {"tags": ["1girl", "solo", "white hair"]}。
不要输出 JSON 以外的任何文字，不要解释，不要用代码块包起来。
要求：
- 每个 tag 是 tags 数组里的一个字符串。
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


MAX_ATTEMPTS = 3
MAX_CJK_RATIO = 0.5  # 中文字符占比超过这个值就不当成 tag


def parse_tags_json(reply: str) -> str | None:
    """从回复里取出 {"tags": [...]}，返回清洗后的 tag 串；格式不对或不像英文 tag 时返回 None。"""
    text = llm.THINK_BLOCK.sub("", reply or "")
    # 模型可能在 JSON 前后加客套话或代码块，取第一个 { 到最后一个 } 之间的部分。
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    raw = data.get("tags") if isinstance(data, dict) else None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        return None
    tags = clean_tags(", ".join(raw))
    if not tags:
        return None
    # tag 应该是英文；大部分是中文时多半是模型把拒绝或解释塞进了 JSON。
    letters = re.sub(r"[\W\d_]", "", tags)
    if not letters or len(_CJK.findall(letters)) / len(letters) > MAX_CJK_RATIO:
        return None
    return tags


async def to_novelai_tags(prompt: str) -> str:
    """让大模型按 JSON 输出 tag；格式不对或正文为空就静默重试，最多 MAX_ATTEMPTS 次。
    接口报错（没配密钥、HTTP 错误）和 max_tokens 不够导致的截断不重试，直接抛出。"""
    messages = [{"role": "system", "content": TRANSLATE_PROMPT}, {"role": "user", "content": prompt}]
    last_problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # max_tokens 用「大模型」里的设置：带思考的模型会先把 token 花在思考上，给太少时正文是空的
        try:
            reply = await llm.chat(messages, temperature=0.3)
        except llm.EmptyReplyError as e:
            if not e.retryable:
                raise ServiceError(f"翻译 tag 失败：{e}") from None
            last_problem = str(e)
            logger.warning("[AI画图] 第 {}/{} 次翻译 tag 没有正文：{}", attempt, MAX_ATTEMPTS, e)
            continue
        tags = parse_tags_json(reply)
        if tags:
            return tags
        last_problem = f"没有按格式返回 tag，最后一次回复「{reply.strip()[:40]}」"
        logger.warning("[AI画图] 第 {}/{} 次翻译 tag 没有按 JSON 返回：{!r}", attempt, MAX_ATTEMPTS, reply[:200])
    raise ServiceError(
        f"翻译 tag 失败：大模型（{llm.default_model()}）连续 {MAX_ATTEMPTS} 次都不行，{last_problem}。"
        "可以在 WebUI「大模型」里换一个模型，或者直接用英文 tag 画"
    )


def join_prompt(prefix: str, prompt: str) -> str:
    prefix = prefix.strip().rstrip(",").strip()
    return f"{prefix}, {prompt}" if prefix else prompt
