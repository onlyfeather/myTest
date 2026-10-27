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


def test_tts_tool_singing_only_for_superuser(sent, monkeypatch):
    from types import SimpleNamespace

    import mytest.plugins.tools_api_plugin.tools as tools
    from mytest.plugins.tts_plugin import service

    monkeypatch.setattr(service, "tts_settings", SimpleNamespace(
        enabled=True, api_key="k", max_chars=10, superuser_max_chars=50,
    ))
    monkeypatch.setattr(tools, "_is_superuser", lambda user_id: user_id == "1196477724")
    spawned = []
    monkeypatch.setattr(tools, "_spawn", lambda name, coro: (spawned.append(name), coro.close()))

    guest = ToolContext("123456", "654321")
    with pytest.raises(ToolError, match="唱歌"):
        asyncio.run(tools.TOOLS["tts"].run(guest, {"text": "啦啦啦", "sing": True}))
    with pytest.raises(ToolError, match="管理员"):
        asyncio.run(tools.TOOLS["tts"].run(guest, {"text": "字" * 11}))
    asyncio.run(tools.TOOLS["tts"].run(guest, {"text": "[happy] 晚安"}))

    owner = ToolContext("1196477724", "654321")
    asyncio.run(tools.TOOLS["tts"].run(owner, {"text": "啦啦啦", "sing": True}))
    asyncio.run(tools.TOOLS["tts"].run(owner, {"text": "[calm] " + "字" * 50}))
    assert spawned == ["tts", "tts", "tts"] and sent == []


def test_draw_tool_validates_before_sending(sent):
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    ctx = ToolContext("123456", "654321")
    with pytest.raises(ToolError, match="描述"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "  "}))
    with pytest.raises(ToolError, match="size"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "猫", "size": "巨图"}))
    with pytest.raises(ToolError, match="太长"):
        asyncio.run(TOOLS["draw"].run(ctx, {"prompt": "猫" * 5001}))
    assert sent == [], "nothing is sent when arguments are invalid"


def test_selfie_requires_persona_image(sent):
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    with pytest.raises(ToolError, match="人设图"):
        asyncio.run(TOOLS["selfie"].run(ToolContext("123456", None), {"scene": "在厨房泡茶"}))
    assert sent == []


def test_selfie_uses_v45_even_when_drawing_uses_v5(monkeypatch, tmp_path):
    """自拍要角色参考，只有 V4.5 支持；画图换成 V5 不能让自拍跟着失败。"""
    from types import SimpleNamespace

    from PIL import Image

    from mytest.plugins._shared.services import novelai
    from mytest.plugins.ai_draw_plugin import config as draw_config
    from mytest.plugins.tools_api_plugin import selfie

    persona = tmp_path / "rinne_persona.png"
    Image.new("RGB", (64, 96), "white").save(persona)
    monkeypatch.setattr(selfie, "persona_path", lambda: persona)
    monkeypatch.setattr(selfie, "selfie_settings", SimpleNamespace(
        enabled=True, backend="novelai", base_prompt="1girl", with_style=True, novelai_model="nai-diffusion-5-full",
        artists="artist:selfie_only",
    ))
    nai = {k: getattr(draw_config.novelai_settings, k) for k in (
        "token", "base_url", "negative", "steps", "scale", "sampler", "positive_prefix", "default_artists",
        "reference_strength", "reference_fidelity", "timeout",
    )}
    monkeypatch.setattr(draw_config, "novelai_settings",
                        SimpleNamespace(**nai, model="nai-diffusion-5-full", translate=False))
    used = {}

    async def fake_generate(req, **kwargs):
        used["model"] = req.model
        used["prompt"] = req.prompt
        return b"png", 1

    monkeypatch.setattr(novelai, "generate", fake_generate)

    assert selfie.selfie_ready() == ""
    assert asyncio.run(selfie.take_selfie("office")) == b"png"
    # 设置里存了不支持参考的模型（例如旧配置）时退回 V4.5
    assert used["model"] in novelai.REFERENCE_MODELS
    assert "artist:selfie_only" in used["prompt"]


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
    bot_self = {**message, "message_info": {"user_info": {"user_id": "3974127672"}}}
    assert mod.build_context(bot_self) is None


def _tools_plugin(mod, messages):
    from types import SimpleNamespace

    logs: list[str] = []
    queries: list[tuple[str, str]] = []

    async def get_by_id(message_id, *, stream_id=""):
        queries.append((message_id, stream_id))
        return {"success": True, "message": messages.get(message_id)}

    plugin = mod.RinneToolsPlugin()
    logger = SimpleNamespace(
        info=lambda *a: logs.append(a[0] % a[1:]), warning=lambda *a: logs.append(a[0] % a[1:]),
        debug=lambda *a: None,
    )
    plugin._set_context(SimpleNamespace(message=SimpleNamespace(get_by_id=get_by_id), logger=logger))
    return plugin, logs, queries


def test_rinne_tools_resolves_sender_from_msg_id():
    """MaiBot 1.3 调用工具时只给 stream_id/group_id，不给消息本身，要按 msg_id 查。"""
    mod = _load_rinne_tools()
    info = {"user_info": {"user_id": "1196477724", "user_cardname": "千树与梦"},
            "group_info": {"group_id": "123456"}}
    messages = {
        "555": {"message_id": "555", "account_id": "3974127672", "message_info": info,
                "raw_message": [{"type": "reply", "data": {"target_message_id": "554"}},
                                {"type": "text", "data": "照着这张画"}]},
        "554": {"message_id": "554", "message_info": info,
                "raw_message": [{"type": "image", "data": "https://gchat.qpic.cn/q"}]},
    }
    plugin, _, queries = _tools_plugin(mod, messages)
    group_kwargs = {"stream_id": "s1", "chat_id": "s1", "group_id": "123456", "platform": "qq"}

    ctx, _ = asyncio.run(plugin._resolve_context({**group_kwargs, "msg_id": "555"}))
    assert ctx == {"user_id": "1196477724", "group_id": "123456", "user_name": "千树与梦",
                   "message_id": "555", "image_urls": ["https://gchat.qpic.cn/q"]}
    assert queries == [("555", "s1"), ("554", "s1")]

    # 群聊里没有 msg_id：不能猜是谁
    ctx, reason = asyncio.run(plugin._resolve_context(group_kwargs))
    assert ctx is None and "msg_id" in reason
    ctx, reason = asyncio.run(plugin._resolve_context({**group_kwargs, "msg_id": "999"}))
    assert ctx is None and "999" in reason

    # 私聊：聊天流里的 user_id 就是对方
    ctx, _ = asyncio.run(plugin._resolve_context({"stream_id": "s2", "user_id": "1196477724", "msg_id": "999"}))
    assert ctx["user_id"] == "1196477724" and ctx["group_id"] is None


def test_rinne_tools_call_logs_and_skips_unknown_requester():
    mod = _load_rinne_tools()
    plugin, logs, _ = _tools_plugin(mod, {})
    cfg = mod.RinneToolsConfig()
    cfg.plugin.token = TOKEN
    plugin._cfg = lambda: cfg
    result = asyncio.run(plugin.rinne_selfie(scene="办公室", stream_id="s1", group_id="123456"))
    assert result["success"] is False and "msg_id" in result["content"]
    assert any("selfie 未执行" in line for line in logs)


def test_rinne_tools_msg_id_not_forwarded(monkeypatch):
    mod = _load_rinne_tools()
    info = {"user_info": {"user_id": "1196477724"}, "group_info": {"group_id": "123456"}}
    plugin, _, _ = _tools_plugin(mod, {"555": {"message_id": "555", "message_info": info, "raw_message": []}})
    cfg = mod.RinneToolsConfig()
    cfg.plugin.token = TOKEN
    plugin._cfg = lambda: cfg
    sent = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            sent.update(url=url, body=json)
            return type("R", (), {"json": lambda self: {"ok": True, "content": "开始拍了"}})()

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(plugin.rinne_selfie(scene="办公室", msg_id="555", stream_id="s1", group_id="123456"))
    assert result == {"success": True, "content": "开始拍了"}
    assert sent["url"].endswith("/tools/selfie")
    assert sent["body"]["args"] == {"scene": "办公室"}
    assert sent["body"]["user_id"] == "1196477724" and sent["body"]["message_id"] == "555"


def test_rinne_tools_declare_msg_id():
    mod = _load_rinne_tools()
    from maibot_sdk.components import collect_components

    tools = [c for c in collect_components(mod.RinneToolsPlugin()) if c.get("type", "").upper() == "TOOL"
             or c.get("component_type", "").upper() == "TOOL"]
    assert tools
    for tool in tools:
        assert "msg_id" in json.dumps(tool, ensure_ascii=False, default=str), tool.get("name")


# ---------------------------------------------------------------- 说说


def test_qzone_ai_flavor_checks():
    from mytest.plugins.tools_api_plugin.qzone import ai_flavor_problems

    assert ai_flavor_problems("红茶罐见底了。明明上周才买的。") == []
    assert ai_flavor_problems("下班。头发终于可以放下来了") == []
    assert ai_flavor_problems("今天也要加油哦～")
    assert ai_flavor_problems("这不是普通的下午，而是属于自己的时间")
    assert ai_flavor_problems("好冷！好冷！")
    assert ai_flavor_problems("大家今天吃什么？")
    assert ai_flavor_problems("字" * 141)
    assert ai_flavor_problems("下雨了☔🌧")


def test_qzone_parse_post():
    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.tools_api_plugin.qzone import parse_post

    post = parse_post('好的\n```json\n{"text": "“红茶罐见底了。”", "mood": "有点无奈", "scene": "厨房里拿着空茶罐"}\n```')
    assert post.text == "红茶罐见底了。" and post.mood == "有点无奈"
    assert "厨房" in post.selfie_scene and "有点无奈" in post.selfie_scene
    for bad in ("没有 JSON", '{"text": ""}', "[1, 2]", "{坏的}"):
        with pytest.raises(ServiceError):
            parse_post(bad)


def _qzone_env(monkeypatch, tmp_path, replies):
    from types import SimpleNamespace

    from mytest.plugins._shared.services import llm
    from mytest.plugins.tools_api_plugin import qzone

    monkeypatch.setattr(qzone, "qzone_settings", SimpleNamespace(
        enabled=True, model="", topics=["擦银器", "停电", "烤司康"], extra="",
    ))
    monkeypatch.setattr(qzone, "history_path", lambda: tmp_path / "qzone_history.json")
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return replies[len(calls) - 1]

    monkeypatch.setattr(llm, "chat", fake_chat)
    return qzone, calls


def test_qzone_write_post_retries_ai_flavor(monkeypatch, tmp_path):
    good = '{"text": "停电了。银器只擦了一半", "mood": "有点烦", "scene": "烛光下擦银器"}'
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [
        '{"text": "生活就是这样美好～", "mood": "开心", "scene": "窗边"}', good,
    ])
    qzone.remember("红茶罐见底了。")
    post = asyncio.run(qzone.write_post(mood="有点烦"))
    assert post.text == "停电了。银器只擦了一半"
    assert len(calls) == 2
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert "白鸟" in system and "红茶罐见底了。" in system
    assert "有点烦" in user
    # 第二次请求带上了要改的地方
    assert "生活就是" in calls[1][-1]["content"]
    # 写成了也不记历史：自拍画好之后才记
    assert [h["text"] for h in qzone.load_history()] == ["红茶罐见底了。"]


def test_qzone_write_post_gives_up(monkeypatch, tmp_path):
    from mytest.plugins._shared.services.http import ServiceError

    bad = '{"text": "今天也要加油！！", "mood": "", "scene": "窗边"}'
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [bad] * 10)
    with pytest.raises(ServiceError, match="AI 腔"):
        asyncio.run(qzone.write_post())
    assert len(calls) == qzone.MAX_ATTEMPTS
    assert qzone.load_history() == []


def test_qzone_history_keeps_latest(monkeypatch, tmp_path):
    qzone, _ = _qzone_env(monkeypatch, tmp_path, [])
    for i in range(qzone.HISTORY_SIZE + 3):
        qzone.remember(f"第{i}条")
    items = qzone.load_history()
    assert len(items) == qzone.HISTORY_SIZE and items[-1]["text"] == f"第{qzone.HISTORY_SIZE + 2}条"


def test_qzone_tool_superuser_only_and_returns_image(sent, monkeypatch):
    import base64

    import mytest.plugins.tools_api_plugin.tools as tools
    from mytest.plugins.tools_api_plugin import qzone, selfie

    monkeypatch.setattr(tools, "_is_superuser", lambda user_id: user_id == "1196477724")
    monkeypatch.setattr(qzone, "qzone_ready", lambda: "")
    monkeypatch.setattr(selfie, "selfie_ready", lambda: "")
    remembered, scenes, asked = [], [], {}

    async def fake_write(**kwargs):
        asked.update(kwargs)
        return qzone.Post(text="停电了。", mood="有点烦", scene="烛光下")

    async def fake_selfie(scene):
        scenes.append(scene)
        return b"png"

    monkeypatch.setattr(qzone, "write_post", fake_write)
    monkeypatch.setattr(qzone, "remember", lambda text, now=None: remembered.append(text))
    monkeypatch.setattr(selfie, "take_selfie", fake_selfie)

    with pytest.raises(ToolError, match="主人"):
        asyncio.run(tools.TOOLS["qzone"].run(ToolContext("123456", "654321"), {}))

    owner = ToolContext("1196477724", "654321")
    assert asyncio.run(tools.TOOLS["qzone"].run(owner, {"check_only": True}))["content"] == "可以发"
    assert scenes == [] and remembered == []

    result = asyncio.run(tools.TOOLS["qzone"].run(owner, {"mood": "心情" * 20, "topic": "下雪"}))
    assert result["text"] == "停电了。" and base64.b64decode(result["image"]) == b"png"
    assert asked == {"topic": "下雪", "mood": ("心情" * 20)[:20]}
    assert scenes == ["烛光下。表情和气氛：有点烦"] and remembered == ["停电了。"]
    assert sent == [], "说说不发到聊天里"

    monkeypatch.setattr(qzone, "qzone_ready", lambda: "发说说功能已关闭")
    with pytest.raises(ToolError, match="关闭"):
        asyncio.run(tools.TOOLS["qzone"].run(owner, {"check_only": True}))


def test_rinne_tools_plan_slots():
    from datetime import date

    mod = _load_rinne_tools()
    day = date(2026, 10, 28)
    rng = random.Random(1)
    assert mod.plan_slots(day, ["10:30", "21:30"], 40, 0.0, rng) == []
    slots = mod.plan_slots(day, ["21:30", "10:30", "25:00", "abc", "9:61"], 40, 1.0, rng)
    assert len(slots) == 2 and slots[0] < slots[1]
    for slot, (h, m) in zip(slots, [(10, 30), (21, 30)], strict=True):
        base = slot.replace(hour=h, minute=m, second=0, microsecond=0)
        assert abs((slot - base).total_seconds()) <= 40 * 60
        assert slot.utcoffset().total_seconds() == 8 * 3600
    exact = mod.plan_slots(day, ["08:00"], 0, 1.0, rng)
    assert exact[0].hour == 8 and exact[0].minute == 0


def _qzone_plugin(mod, monkeypatch, api_result, nonebot_replies):
    from types import SimpleNamespace

    plugin, logs, _ = _tools_plugin(mod, {})
    cfg = mod.RinneToolsConfig()
    cfg.plugin.token = TOKEN
    plugin._cfg = lambda: cfg
    plugin._jobs = set()
    plugin._posting = asyncio.Lock()
    calls, texts, requests = [], [], []

    async def api_call(name, **kwargs):
        calls.append((name, kwargs))
        if isinstance(api_result, Exception):
            raise api_result
        return api_result

    async def send_text(text, stream_id, **kwargs):
        texts.append((text, stream_id))
        return True

    async def fake_request(tool, body, timeout):
        requests.append((tool, body))
        return nonebot_replies[len(requests) - 1]

    plugin._ctx.api = SimpleNamespace(call=api_call)
    plugin._ctx.send = SimpleNamespace(text=send_text)
    monkeypatch.setattr(plugin, "_request", fake_request)
    return plugin, logs, calls, texts, requests


def test_rinne_tools_qzone_post_flow(monkeypatch):
    import base64

    mod = _load_rinne_tools()
    image = base64.b64encode(b"png").decode()
    plugin, logs, calls, texts, requests = _qzone_plugin(mod, monkeypatch, {"result": True, "message": "ok"}, [
        {"ok": True, "content": "可以发"},
        {"ok": True, "content": "说说已经写好", "text": "停电了。", "mood": "有点烦", "image": image},
    ])

    async def run():
        result = await plugin.rinne_qzone_post(mood="有点烦", stream_id="s2", user_id="1196477724")
        await asyncio.gather(*plugin._jobs)
        return result

    result = asyncio.run(run())
    assert result["success"] is True and "QQ 空间" in result["content"]
    assert requests[0] == ("qzone", requests[0][1]) and requests[0][1]["args"] == {"mood": "有点烦", "check_only": True}
    assert requests[1][1]["args"] == {"mood": "有点烦"} and requests[1][1]["user_id"] == "1196477724"
    assert calls == [(mod.MAIZONE_API, {"message": "停电了。", "images": [b"png"]})]
    assert texts == []


def test_rinne_tools_qzone_refused_and_failures(monkeypatch):
    mod = _load_rinne_tools()
    plugin, _, calls, _, requests = _qzone_plugin(mod, monkeypatch, {"result": True}, [
        {"ok": False, "message": "发说说只有主人（管理员）能让你发"},
    ])
    result = asyncio.run(plugin.rinne_qzone_post(stream_id="s2", user_id="42"))
    assert result["success"] is False and "主人" in result["content"]
    assert len(requests) == 1 and plugin._jobs == set() and calls == []

    # Maizone 报错 / 没装：后台任务在会话里说一声
    import base64

    image = base64.b64encode(b"png").decode()
    reply = {"ok": True, "text": "停电了。", "image": image}
    for api_result, expect in (({"result": False, "message": "cookie 失效"}, "cookie 失效"),
                               (RuntimeError("API 不存在"), "Maizone")):
        plugin, logs, _, texts, _ = _qzone_plugin(mod, monkeypatch, api_result, [reply])
        asyncio.run(plugin._qzone_job({"user_id": "1196477724", "args": {}}, "s2"))
        assert len(texts) == 1 and expect in texts[0][0] and texts[0][1] == "s2"

    plugin, _, calls, texts, _ = _qzone_plugin(mod, monkeypatch, {"result": True}, [{"ok": True, "text": "停电了。"}])
    asyncio.run(plugin._qzone_job({"user_id": "1196477724", "args": {}}, "s2"))
    assert calls == [] and "图片" in texts[0][0]


def test_rinne_tools_scheduler_needs_owner():
    mod = _load_rinne_tools()

    async def run(owner_id):
        plugin, logs, _ = _tools_plugin(mod, {})
        cfg = mod.RinneToolsConfig()
        cfg.qzone.auto_enabled = True
        cfg.qzone.owner_id = owner_id
        plugin._cfg = lambda: cfg
        plugin._restart_scheduler()
        task = plugin._scheduler_task
        if task is not None:
            task.cancel()
        return task, logs

    task, logs = asyncio.run(run(""))
    assert task is None and any("owner_id" in line for line in logs)
    task, _ = asyncio.run(run("1196477724"))
    assert task is not None
