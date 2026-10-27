"""工具接口、确认词、凛音工具插件的消息解析。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mytest.plugins._shared.phrases import DEFAULT_FILE, PhraseBook
from mytest.plugins.tools_api_plugin.server import create_router, is_local
from mytest.plugins.tools_api_plugin.tools import ToolContext, ToolError, ToolSpec

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 32


# ---------------------------------------------------------------- 确认词


def test_phrase_book_groups_are_distinct_and_non_repeating():
    book = PhraseBook(DEFAULT_FILE, rng=random.Random(0))
    groups = book.groups()
    for key in ("draw", "draw_reference", "selfie", "tts", "dice", "pixiv_search", "pixiv_daily", "busy"):
        assert len(groups.get(key, [])) >= 3, key
    # 不同功能的词不重叠，保证有区分度
    seen: dict[str, str] = {}
    for key, phrases in groups.items():
        for phrase in phrases:
            assert phrase not in seen, f"「{phrase}」同时出现在 {seen.get(phrase)} 和 {key}"
            seen[phrase] = key
    picks = [book.pick("draw") for _ in range(50)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False)), "never the same phrase twice in a row"
    assert set(picks) <= set(groups["draw"])


def test_phrase_book_fallbacks(tmp_path):
    book = PhraseBook(tmp_path / "missing.json")
    assert book.pick("draw") == "请稍候。"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert PhraseBook(bad).pick("x") == "请稍候。"
    custom = tmp_path / "c.json"
    custom.write_text(json.dumps({"_说明": ["ignored"], "draw": ["好的，{name}。"], "default": ["默认"]}), encoding="utf-8")
    book = PhraseBook(custom)
    assert book.pick("draw", name="主人") == "好的，主人。"
    assert book.pick("draw") == "好的，您。"
    assert book.pick("unknown") == "默认"
    assert "_说明" not in book.groups()


def test_phrases_match_persona():
    """确认词是凛音工作中的口吻：群聊里也会出现，所以不能有「呆货」「本小姐」，也不用波浪号和颜文字。"""
    for group, phrases in PhraseBook(DEFAULT_FILE).groups().items():
        for phrase in phrases:
            for banned in ("呆货", "本小姐", "～", "~", "♪", "w"):
                assert banned not in phrase, (group, phrase)


# ---------------------------------------------------------------- 上下文校验


def test_tool_context_validation():
    ctx = ToolContext.from_body({
        "user_id": "123456", "group_id": "654321", "user_name": "千树",
        "message_id": "789", "image_urls": ["https://gchat.qpic.cn/x", "http://127.0.0.1/evil", 5],
    })
    assert (ctx.user_id, ctx.group_id, ctx.reply_to) == ("123456", "654321", "789")
    assert ctx.image_urls == ["https://gchat.qpic.cn/x"], "non-QQ image hosts are dropped"
    assert ToolContext.from_body({"user_id": "123456"}).group_id is None
    for bad in ({}, {"user_id": "abc"}, {"user_id": "123456", "group_id": "1; drop"}):
        with pytest.raises(ToolError):
            ToolContext.from_body(bad)


def test_tool_spec_rejects_unknown_and_missing_args():
    async def handler(ctx, args):
        return {"content": "ok"}

    spec = ToolSpec("x", "x", {"prompt": {"type": "string", "required": True}, "size": {"type": "string"}}, handler)
    ctx = ToolContext("123456", None)
    assert asyncio.run(spec.run(ctx, {"prompt": "猫"})) == {"content": "ok"}
    with pytest.raises(ToolError, match="缺少"):
        asyncio.run(spec.run(ctx, {"size": "竖图"}))
    with pytest.raises(ToolError, match="未知"):
        asyncio.run(spec.run(ctx, {"prompt": "猫", "admin": True}))


# ---------------------------------------------------------------- HTTP 接口


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1", True), ("::1", True), ("172.17.0.1", True), ("192.168.1.5", True), ("10.0.0.2", True),
    ("::ffff:127.0.0.1", True),
    ("8.8.8.8", False), ("203.0.113.9", False), ("172.32.0.1", False), ("100.64.0.1", False),
    (None, False), ("testclient", False),
])
def test_is_local(host, ok):
    assert is_local(host) is ok


def make_client(client_host: str = "127.0.0.1", token: str = TOKEN):
    calls = []

    async def handler(ctx, args):
        calls.append((ctx, args))
        return {"content": "已经开始了", "stop_after_execution": True}

    tools = {"draw": ToolSpec("draw", "画图", {"prompt": {"type": "string", "required": True}}, handler)}
    app = FastAPI()
    app.include_router(create_router(tools, lambda: token))
    return TestClient(app, client=(client_host, 50000)), calls


def test_api_auth_and_call():
    client, calls = make_client()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = {"user_id": "123456", "group_id": "654321", "args": {"prompt": "猫"}}
    assert client.post("/tools/draw", json=body).status_code == 401
    assert client.post("/tools/draw", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.post("/tools/draw", json=body, headers=auth)
    assert r.json() == {"ok": True, "content": "已经开始了", "stop_after_execution": True}
    assert calls[0][0].group_id == "654321" and calls[0][1] == {"prompt": "猫"}
    assert client.get("/tools", headers=auth).json()["tools"][0]["name"] == "draw"
    assert client.post("/tools/nope", json=body, headers=auth).status_code == 404
    r = client.post("/tools/draw", json={"user_id": "x", "args": {}}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is False and "QQ" in r.json()["message"]
    assert client.post("/tools/draw", json=[1], headers=auth).status_code == 400


def test_api_rejects_public_clients_and_missing_token():
    client, calls = make_client(client_host="8.8.8.8")
    r = client.post("/tools/draw", json={"user_id": "123456", "args": {"prompt": "x"}},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403 and calls == []
    client, _ = make_client(token="")
    assert client.get("/tools", headers={"Authorization": "Bearer "}).status_code == 503


# ---------------------------------------------------------------- 真实工具（打桩发送）


@pytest.fixture
def sent(monkeypatch):
    import mytest.plugins.tools_api_plugin.tools as tools

    messages = []

    async def fake_send(ctx, message, *, quote=False):
        messages.append((ctx.group_id, str(message), quote))

    monkeypatch.setattr(tools, "send", fake_send)
    return messages


def test_dice_tool_sends_result_and_returns_numbers(sent):
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    ctx = ToolContext("123456", "654321")
    result = asyncio.run(TOOLS["dice"].run(ctx, {"faces": 20, "target": 15, "event": "侦查"}))
    assert 1 <= result["roll"] <= 20 and result["status"] in {"大成功", "成功", "失败", "大失败"}
    assert "不能更改" in result["content"]
    assert len(sent) == 1 and "侦查：1D20=" in sent[0][1] and sent[0][2] is True
    with pytest.raises(ToolError):
        asyncio.run(TOOLS["dice"].run(ctx, {"faces": "abc"}))


def test_draw_tool_validates_before_sending(sent):
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    ctx = ToolContext("123456", "654321")
    with pytest.raises(ToolError, match="描述"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "  "}))
    with pytest.raises(ToolError, match="size"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "猫", "size": "巨图"}))
    with pytest.raises(ToolError, match="太长"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "猫" * 600}))
    assert sent == [], "nothing is sent when arguments are invalid"


def test_selfie_requires_persona_image(sent):
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    with pytest.raises(ToolError, match="人设图"):
        asyncio.run(TOOLS["selfie"].run(ToolContext("123456", None), {"scene": "在厨房泡茶"}))
    assert sent == []


# ---------------------------------------------------------------- MaiBot 插件的消息解析


def _load_rinne_tools():
    pytest.importorskip("maibot_sdk")
    spec = importlib.util.spec_from_file_location("rinne_tools_plugin", ROOT / "maibot_plugins/rinne_tools/plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rinne_tools_build_context():
    mod = _load_rinne_tools()
    message = {
        "message_id": "1147268509",
        "account_id": "3974127672",
        "message_info": {
            "user_info": {"user_id": "42", "user_nickname": "nick", "user_cardname": "千树与梦"},
            "group_info": {"group_id": "123456", "group_name": "测试群"},
        },
        "raw_message": [
            {"type": "text", "data": "照着这张画"},
            {"type": "image", "data": {"url": "https://gchat.qpic.cn/a"}},
            {"type": "reply", "data": [{"type": "image", "data": "https://multimedia.nt.qq.com.cn/b"}]},
        ],
    }
    ctx = mod.build_context(message)
    assert ctx == {
        "user_id": "42", "group_id": "123456", "user_name": "千树与梦", "message_id": "1147268509",
        "image_urls": ["https://gchat.qpic.cn/a", "https://multimedia.nt.qq.com.cn/b"],
    }
    private = mod.build_context({"message": {**message, "message_info": {"user_info": {"user_id": "42"}}}})
    assert private["group_id"] is None and private["image_urls"]
    assert mod.build_context({}) is None
