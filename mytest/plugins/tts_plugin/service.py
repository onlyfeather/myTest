from __future__ import annotations

import re

from .._shared.services import fish_audio
from .._shared.services.http import ServiceError
from .config import tts_settings

# 骰娘点评里的动作描写（括号里的内容）和表情符号读出来很怪，合成前去掉。
_BRACKETS = re.compile(r"[（(【\[*][^）)】\]*]{0,40}[）)】\]*]")
_SYMBOLS = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF~～]+")


def speakable(text: str) -> str:
    text = _BRACKETS.sub("", text)
    text = _SYMBOLS.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_ready() -> bool:
    return bool(tts_settings.enabled and str(tts_settings.api_key).strip())


async def synthesize(text: str) -> bytes:
    s = tts_settings
    text = speakable(text)
    if not text:
        raise ServiceError("没有可以朗读的文字")
    limit = int(s.max_chars)
    if len(text) > limit:
        raise ServiceError(f"文字太长了，最多 {limit} 个字")
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


async def test_tts() -> str:
    audio = await synthesize("测试")
    return f"合成成功，音频 {len(audio) // 1024 or 1} KB"
