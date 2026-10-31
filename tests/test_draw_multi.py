"""多图参考：一张一张收图，图一、图二……。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio

from mytest.plugins.ai_draw_plugin import multi
from mytest.plugins.ai_draw_plugin.prompting import parse_request


def run(replies, *, first=None, limit=multi.MAX_IMAGES):
    """按顺序回复 replies，返回 (结果, 机器人问的话, 机器人说的话, 下载过的地址)。"""
    asked, said, downloaded = [], [], []
    queue = list(replies)

    async def ask(text):
        asked.append(text)
        return queue.pop(0) if queue else None

    async def download(url):
        downloaded.append(url)
        return url.encode()

    async def say(text):
        said.append(text)

    result = asyncio.run(multi.collect(ask, download, say, first=first, limit=limit))
    return result, asked, said, downloaded


def test_flag_parsed_and_removed():
    req = parse_request("-多图 图一的人穿着图二的衣服")
    assert req.wants_multi and req.prompt == "图一的人穿着图二的衣服"
    assert parse_request("-multi 猫").wants_multi
    assert not parse_request("猫").wants_multi


def test_labels():
    assert [multi.label(i) for i in range(4)] == ["图一", "图二", "图三", "图四"]
    assert multi.label(10) == "图11"
    assert multi.describe(0) == ""
    assert multi.describe(1) == "参考了 1 张图"
    assert multi.describe(3) == "参考了 3 张图（图一到图三）"


def test_collect_one_by_one_until_done():
    result, asked, said, _ = run([("", ["u1"]), ("", ["u2"]), ("好了", [])])
    assert result.images == [b"u1", b"u2"] and not result.cancelled
    assert asked[0].startswith("请发送图一")
    assert asked[1].startswith("收到图一。请发送图二")
    assert asked[2].startswith("收到图二。请发送图三")
    assert said == []


def test_attached_image_counts_as_first():
    result, asked, _, downloaded = run([("", ["u2"]), ("ok", [])], first=[b"u1"])
    assert result.images == [b"u1", b"u2"] and downloaded == ["u2"]
    assert asked[0].startswith("收到图一。请发送图二")


def test_stops_at_limit():
    result, asked, said, _ = run([("", [f"u{i}"]) for i in range(6)])
    assert result.images == [b"u0", b"u1", b"u2", b"u3"]
    assert len(asked) == 4
    assert said[-1] == "收到图四，已经 4 张了，开始画"


def test_only_first_image_of_a_message_is_used():
    result, _, said, downloaded = run([("", ["a", "b", "c"]), ("好了", [])])
    assert result.images == [b"a"] and downloaded == ["a"]
    assert "只取了第一张当图一" in said[0]


def test_cancel_and_timeout():
    result, *_ = run([("", ["u1"]), ("取消", [])])
    assert result.cancelled
    result, *_ = run([("", ["u1"])])  # 第二次没回复（超时）
    assert result.cancelled


def test_done_without_images_is_not_accepted():
    result, asked, *_ = run([("好了", []), ("", ["u1"]), ("好了", [])])
    assert result.images == [b"u1"]
    assert asked[1].startswith("这条消息里没有图片。要先发一张图")


def test_gives_up_after_repeated_misses():
    result, *_ = run([("你好", []), ("？", [])])
    assert result.cancelled and result.images == []
    result, *_ = run([("", ["u1"]), ("你好", []), ("？", [])])
    assert not result.cancelled and result.images == [b"u1"], "已经有图就用已有的"


def test_gpt_caps_references(monkeypatch):
    from mytest.plugins.ai_draw_plugin import service

    seen = {}

    async def fake_generate(prompt, **kwargs):
        seen["refs"] = kwargs["references"]
        return b"OUT"

    monkeypatch.setattr(service.gpt_image, "generate", fake_generate)
    monkeypatch.setattr(service.images, "normalize", lambda img: (img, None))
    req = parse_request("-多图 图一的人穿着图二的衣服")
    inputs = service.DrawInputs(references=[b"1", b"2", b"3", b"4", b"5"])
    result = asyncio.run(service.draw_gpt(req, inputs))
    assert seen["refs"] == [b"1", b"2", b"3", b"4"]
    assert result.note == "参考了 4 张图（图一到图四）"
