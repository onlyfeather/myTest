from __future__ import annotations

import re

from .._shared.services import fish_audio, model_list
from .._shared.services.http import ServiceError
from .config import tts_settings

# 骰娘点评里的动作描写（括号里的内容）和表情符号读出来很怪，合成前去掉。
_BRACKETS = re.compile(r"[（(【\[*][^）)】\]*]{0,40}[）)】\]*]")
# Drama 3 把 [半角方括号] 当作演绎指导（语气、停顿、叹气等），要保留下来。
_BRACKETS_KEEP_DIRECTIONS = re.compile(r"[（(【*][^）)】*]{0,40}[）)】*]")
_SYMBOLS = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF~～]+")
_DIRECTION = re.compile(r"\[[^\]]*\]")
_SINGING = re.compile(r"\[[^\]]*(?:sing|唱)[^\]]*\]", re.IGNORECASE)


def is_singing(text: str) -> bool:
    return bool(_SINGING.search(text))


def spoken_length(text: str) -> int:
    """要读出来的字数，[情感提示] 不算。"""
    return len(_DIRECTION.sub("", text).strip())


def char_limit(*, superuser: bool) -> int:
    s = tts_settings
    return max(int(s.superuser_max_chars), int(s.max_chars)) if superuser else int(s.max_chars)


def speakable(text: str, *, keep_directions: bool = False) -> str:
    pattern = _BRACKETS_KEEP_DIRECTIONS if keep_directions else _BRACKETS
    text = pattern.sub("", text)
    text = _SYMBOLS.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if keep_directions and not re.sub(r"\[[^\]]*\]", "", text).strip():
        return ""  # 只有指导、没有要读的字
    return text


def is_ready() -> bool:
    return bool(tts_settings.enabled and str(tts_settings.api_key).strip())


async def synthesize(text: str, *, superuser: bool = False) -> bytes:
    """superuser 可以唱歌、说大段的话；其他人唱歌会被拒绝，字数按 max_chars 限制。"""
    s = tts_settings
    if not superuser and is_singing(text):
        raise ServiceError("唱歌只有管理员可以用")
    text = speakable(text, keep_directions=str(s.model) == fish_audio.DRAMA_MODEL)
    if not text:
        raise ServiceError("没有可以朗读的文字")
    limit = char_limit(superuser=superuser)
    if spoken_length(text) > limit:
        hint = "" if superuser else "，大段语音只有管理员可以用"
        raise ServiceError(f"文字太长了，最多 {limit} 个字{hint}")
    return await fish_audio.synthesize(
        text,
        api_key=str(s.api_key),
        base_url=str(s.base_url),
        model=str(s.model),
        reference_id=str(s.reference_id),
        audio_format=str(s.format),
        speed=float(s.speed),
        volume=float(s.volume),
        proxy=str(s.proxy),
        timeout=float(s.timeout),
    )


async def list_voices() -> list[tuple[str, str]]:
    s = tts_settings
    return await model_list.fish_voices(api_key=str(s.api_key), base_url=str(s.base_url), proxy=str(s.proxy))


async def test_tts() -> str:
    audio = await synthesize("测试")
    return f"合成成功，音频 {len(audio) // 1024 or 1} KB"
