"""画图指令解析、tag 清洗、朗读文本处理。运行：python -m pytest tests -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mytest.plugins.ai_draw_plugin.prompting import clean_tags, has_chinese, join_prompt, parse_request
from mytest.plugins.tts_plugin.service import speakable


def test_parse_request():
    req = parse_request("横图 海边")
    assert (req.size, req.prompt) == ("横图", "海边")
    assert parse_request("方 cat").size == "方图"
    assert parse_request("海边 横图").size == "竖图"
    assert parse_request("").prompt == ""


def test_tags():
    assert has_chinese("1girl, 白发") and not has_chinese("1girl, white hair")
    assert clean_tags("Tags: 1girl，solo,\n white hair, 1girl, ") == "1girl, solo, white hair"
    assert clean_tags("```1girl, solo```") == "1girl, solo"
    assert join_prompt("best quality,", "1girl") == "best quality, 1girl"
    assert join_prompt("  ", "1girl") == "1girl"


def _fake_chat(monkeypatch, *replies):
    """依次返回 replies 里的回复，记录调用次数。"""
    from mytest.plugins._shared.services import llm

    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_parse_tags_json():
    from mytest.plugins.ai_draw_plugin.prompting import parse_tags_json

    assert parse_tags_json('{"tags": ["1girl", "solo", "white hair"]}') == "1girl, solo, white hair"
    assert parse_tags_json('好的：\n```json\n{"tags": ["1girl", "1girl", " solo "]}\n```') == "1girl, solo"
    assert parse_tags_json('<think>用户想要{一个}女孩</think>{"tags": ["1girl"]}') == "1girl"
    assert parse_tags_json('{"tags": "1girl, solo"}') == "1girl, solo"
    assert parse_tags_json('{"tags": ["1girl", "初音ミク", "white hair"]}') == "1girl, 初音ミク, white hair"
    for bad in ("", "1girl, solo", "抱歉，我无法生成这类内容", '{"tags": []}', '{"tag": ["1girl"]}',
                '{"tags": ["抱歉", "我无法生成"]}', '{"tags": [1, 2]}', '{"tags": ["1girl"', "<think>没想完"):
        assert parse_tags_json(bad) is None, bad


def test_translate_retries_silently(monkeypatch):
    import asyncio

    from mytest.plugins.ai_draw_plugin import prompting

    calls = _fake_chat(monkeypatch, "1girl, solo", "抱歉，我不能", '{"tags": ["1girl", "solo"]}')
    assert asyncio.run(prompting.to_novelai_tags("白发少女")) == "1girl, solo"
    assert len(calls) == 3
    assert "max_tokens" not in calls[0], "uses the WebUI setting"


def test_translate_gives_up_after_three(monkeypatch):
    """三次都不按格式返回才报错，不用原文出图。"""
    import asyncio

    import pytest

    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.ai_draw_plugin import prompting

    calls = _fake_chat(monkeypatch, "抱歉，我不能")
    with pytest.raises(ServiceError, match=r"连续 3 次都不行，没有按格式返回 tag.*抱歉"):
        asyncio.run(prompting.to_novelai_tags("白发少女"))
    assert len(calls) == 3


def test_translate_retries_empty_reply(monkeypatch):
    """内容审核、只有思考过程这类空正文会重试；三次都空时报最后一次的原因。"""
    import asyncio

    import pytest

    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins._shared.services.llm import EmptyReplyError
    from mytest.plugins.ai_draw_plugin import prompting

    calls = _fake_chat(monkeypatch, EmptyReplyError("被内容审核拦截了"), '{"tags": ["1girl"]}')
    assert asyncio.run(prompting.to_novelai_tags("白发少女")) == "1girl"
    assert len(calls) == 2

    calls = _fake_chat(monkeypatch, EmptyReplyError("只返回了思考过程"))
    with pytest.raises(ServiceError, match="连续 3 次都不行，只返回了思考过程"):
        asyncio.run(prompting.to_novelai_tags("白发少女"))
    assert len(calls) == 3


def test_translate_truncated_not_retried(monkeypatch):
    """max_tokens 不够时原样重试也一样被截断，直接报错。"""
    import asyncio

    import pytest

    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins._shared.services.llm import EmptyReplyError
    from mytest.plugins.ai_draw_plugin import prompting

    calls = _fake_chat(monkeypatch, EmptyReplyError("输出被截断了", retryable=False))
    with pytest.raises(ServiceError, match=r"^翻译 tag 失败：输出被截断了"):
        asyncio.run(prompting.to_novelai_tags("白发少女"))
    assert len(calls) == 1


def test_translate_api_error_not_retried(monkeypatch):
    import asyncio

    import pytest

    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.ai_draw_plugin import prompting

    calls = _fake_chat(monkeypatch, ServiceError("还没有配置大模型的 API Key"))
    with pytest.raises(ServiceError, match="API Key"):
        asyncio.run(prompting.to_novelai_tags("白发少女"))
    assert len(calls) == 1


def test_speakable():
    assert speakable("（轻轻鼓掌）不愧是您～✨") == "不愧是您"
    assert speakable("*叹气* 好吧【小声】") == "好吧"
    assert speakable("[sigh] 好吧") == "好吧"


def test_speakable_drama3_keeps_directions():
    text = "（轻轻鼓掌）[冷淡又有点不耐烦] 请您自重～ [sigh]"
    assert speakable(text, keep_directions=True) == "[冷淡又有点不耐烦] 请您自重 [sigh]"
    assert speakable("[whisper]（小声）", keep_directions=True) == "", "directions alone are not speakable"


def test_singing_and_long_text_need_superuser(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest

    from mytest.plugins._shared.services import fish_audio
    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.tts_plugin import service

    monkeypatch.setattr(service, "tts_settings", SimpleNamespace(
        model=fish_audio.DRAMA_MODEL, max_chars=10, superuser_max_chars=50, api_key="k", base_url="https://x",
        reference_id="", singing_reference_id="", singing_model="", format="mp3", speed=1.0, volume=0.0,
        proxy="", timeout=5.0,
    ))
    sent = []

    async def fake_synthesize(text, **kwargs):
        sent.append(text)
        return b"mp3"

    monkeypatch.setattr(fish_audio, "synthesize", fake_synthesize)

    # 情感提示不计字数
    assert service.spoken_length("[happy] 一二三四五 [break]") == 5
    asyncio.run(service.synthesize("[happy] 一二三四五六七八九十"))
    assert sent[-1] == "[happy] 一二三四五六七八九十"

    with pytest.raises(ServiceError, match="唱歌"):
        asyncio.run(service.synthesize("[singing] 啦啦啦"))
    with pytest.raises(ServiceError, match="管理员"):
        asyncio.run(service.synthesize("字" * 11))
    asyncio.run(service.synthesize("[singing] 啦啦啦", superuser=True))
    asyncio.run(service.synthesize("字" * 50, superuser=True))
    with pytest.raises(ServiceError, match="50"):
        asyncio.run(service.synthesize("字" * 51, superuser=True))


def test_singing_uses_its_own_voice(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from mytest.plugins._shared.services import fish_audio
    from mytest.plugins.tts_plugin import service

    settings = SimpleNamespace(
        model="s2.1-pro", max_chars=100, superuser_max_chars=100, api_key="k", base_url="https://x",
        reference_id="talk", singing_reference_id="sing", singing_model=fish_audio.DRAMA_MODEL,
        format="mp3", speed=1.0, volume=0.0, proxy="", timeout=5.0,
    )
    monkeypatch.setattr(service, "tts_settings", settings)
    calls = []

    async def fake_synthesize(text, **kwargs):
        calls.append((text, kwargs["model"], kwargs["reference_id"]))
        return b"mp3"

    monkeypatch.setattr(fish_audio, "synthesize", fake_synthesize)
    asyncio.run(service.synthesize("[happy] 你好"))
    assert calls[-1] == ("你好", "s2.1-pro", "talk")
    asyncio.run(service.synthesize("[singing] 啦啦啦", superuser=True))
    assert calls[-1] == ("[singing] 啦啦啦", fish_audio.DRAMA_MODEL, "sing"), "Drama 3 keeps the cue"
    settings.singing_reference_id = settings.singing_model = ""
    asyncio.run(service.synthesize("[singing] 啦啦啦", superuser=True))
    assert calls[-1] == ("啦啦啦", "s2.1-pro", "talk"), "empty singing settings fall back"


def test_drama3_listed():
    from mytest.plugins._shared.services import fish_audio

    assert fish_audio.DRAMA_MODEL in {v for v, _ in fish_audio.MODELS}
