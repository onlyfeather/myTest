"""工具接口、确认词、凛音工具插件的消息解析。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
from datetime import datetime, timedelta
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


def test_dice_tool_uses_lower_is_better(sent, monkeypatch):
    """凛音帮人投的检定和 .ra 一样：点数越小越好。"""
    from mytest.plugins.dice_girl_plugin.dice import engine
    from mytest.plugins.tools_api_plugin.tools import TOOLS

    monkeypatch.setattr(engine.random, "randint", lambda a, b: 8)
    result = asyncio.run(TOOLS["dice"].run(ToolContext("123456", "654321"), {"target": 60, "event": "侦查"}))
    assert result["status"] == "极难成功" and "极难成功" in sent[0][1]


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
    from mytest.plugins.tools_api_plugin.qzone import EXAMPLES, LENGTHS, ai_flavor_problems

    for example in EXAMPLES:
        assert any(ai_flavor_problems(example, length) == [] for length in LENGTHS), example
    assert any("太短" in p for p in ai_flavor_problems("红茶罐见底了。明明上周才买的。"))
    assert ai_flavor_problems("今天也要加油哦～")
    assert ai_flavor_problems("这不是普通的下午，而是属于自己的时间")
    assert ai_flavor_problems("好冷！好冷！")
    assert ai_flavor_problems("大家今天吃什么？")
    assert ai_flavor_problems("字" * 261)
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
        enabled=True, model="", topics=["擦银器", "停电", "烤司康"], extra="", photo_ratio=35, city="",
    ))
    monkeypatch.setattr(qzone, "history_path", lambda: tmp_path / "qzone_history.json")
    # 长度档默认固定成正常长度，测长短的用例自己再换
    monkeypatch.setattr(qzone, "pick_length", lambda rng: qzone.NORMAL)
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return replies[len(calls) - 1]

    monkeypatch.setattr(llm, "chat", fake_chat)
    return qzone, calls


LONG_POST = ("停电了。银器只擦了一半，剩下的只能摸黑擦，擦完也看不出亮没亮。蜡烛翻了半个储物间才找到两根，"
             "一根还是生日蛋糕上拔下来的。冰箱里的黄油大概撑不到来电，晚饭只好改成冷的三明治。"
             "楼下有人在喊物业，喊了三遍，没人应")


def test_qzone_write_post_retries_ai_flavor(monkeypatch, tmp_path):
    good = json.dumps({"text": LONG_POST, "mood": "有点烦", "scene": "烛光下擦银器"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [
        '{"text": "生活就是这样美好～", "mood": "开心", "scene": "窗边"}',
        '{"text": "停电了。银器只擦了一半", "mood": "有点烦", "scene": "烛光下擦银器"}', good,
    ])
    qzone.remember("红茶罐见底了。")
    post = asyncio.run(qzone.write_post(mood="有点烦"))
    assert post.text == LONG_POST
    assert len(calls) == 3
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert "白鸟" in system and "红茶罐见底了。" in system
    assert "有点烦" in user
    # 第二次请求带上了要改的地方
    retries = [m["content"] for m in calls[-1] if m["role"] == "user"][1:]
    assert "生活就是" in retries[0]
    # 太短也要重写
    assert "太短" in retries[1]
    # 写成了也不记历史：自拍画好之后才记
    assert [h["text"] for h in qzone.load_history()] == ["红茶罐见底了。"]


def test_qzone_slot_prompts(monkeypatch, tmp_path):
    from datetime import datetime

    reply = json.dumps({"event": "睡前发现明天要熨的衬衫还没收", "text": LONG_POST, "mood": "困", "scene": "睡衣"},
                       ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply] * 3)
    night = datetime(2026, 10, 28, 23, 10, tzinfo=qzone.BEIJING)
    post = asyncio.run(qzone.write_post(slot="bedtime", now=night))
    assert post.event.startswith("睡前")
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert "深夜，睡前" in system and "不用敬语" in system and "睡衣" in user
    # 定时说说也给题材参考，排在时段提示后面
    assert "从这里挑" in user and user.index("睡衣") < user.index("从这里挑")
    assert any(t in user for t in ("擦银器", "停电", "烤司康"))

    morning = datetime(2026, 10, 28, 9, 50, tzinfo=qzone.BEIJING)
    asyncio.run(qzone.write_post(slot="work", now=morning))
    assert "上午，工作中" in calls[1][0]["content"] and "工作时间" in calls[1][0]["content"]
    # 18 点按人设本来还算上班时间，但「刚下班」时段要用下班口吻
    evening = datetime(2026, 10, 28, 17, 30, tzinfo=qzone.BEIJING)
    asyncio.run(qzone.write_post(slot="off_work", now=evening))
    assert "刚下班" in calls[2][0]["content"] and "下班后" in calls[2][0]["content"]


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


def test_qzone_length_tiers():
    from mytest.plugins.tools_api_plugin import qzone

    short, normal, long_ = qzone.LENGTHS
    # 一个字的说说在短档里合格，在正常档里太短
    assert qzone.ai_flavor_problems("困。", short) == []
    assert any("太短" in p for p in qzone.ai_flavor_problems("困。", normal))
    assert any("太长" in p for p in qzone.ai_flavor_problems(LONG_POST, short))
    assert qzone.ai_flavor_problems(LONG_POST, normal) == []
    assert any("太短" in p for p in qzone.ai_flavor_problems(LONG_POST, long_))
    # AI 腔不管哪个档都要拦
    assert qzone.ai_flavor_problems("加油。", short)
    # 每个档都抽得到，正常档最多
    rng = random.Random(0)
    picks = [qzone.pick_length(rng) for _ in range(1000)]
    assert {picks.count(x) > 0 for x in qzone.LENGTHS} == {True}
    assert picks.count(normal) > picks.count(short) > picks.count(long_)


def test_qzone_short_tier_passes_and_retries_follow_tier(monkeypatch, tmp_path):
    from mytest.plugins.tools_api_plugin import qzone as qzone_mod

    short = qzone_mod.LENGTHS[0]
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [
        json.dumps({"text": LONG_POST, "mood": "困", "scene": "床上"}, ensure_ascii=False),
        '{"text": "困。", "mood": "困", "scene": "床上"}',
    ])
    monkeypatch.setattr(qzone, "pick_length", lambda rng: short)
    post = asyncio.run(qzone.write_post())
    assert post.text == "困。" and len(calls) == 2
    assert "只写一句话" in calls[0][1]["content"]
    assert "太长" in calls[1][-1]["content"] and "只写一句话" in calls[1][-1]["content"]


def test_qzone_examples_rotate():
    from mytest.plugins.tools_api_plugin import qzone

    assert len(qzone.EXAMPLES) >= 10
    lengths = [len(e) for e in qzone.EXAMPLES]
    assert min(lengths) <= 10 and max(lengths) >= 200, "示例要有长有短"
    rng = random.Random(0)
    seen = set()
    for _ in range(200):
        length = qzone.pick_length(rng)
        picked = qzone.pick_examples(length, rng)
        assert len(picked) == 2 and picked[0] != picked[1]
        assert length.low <= len(picked[0]) <= length.high, "第一条合这次的长度档"
        seen.update(picked)
    assert len(seen) >= len(qzone.EXAMPLES) - 1
    # 抽到的示例写进 system prompt，没抽到的不写
    system = qzone.build_system_prompt({}, datetime(2026, 10, 28, 10, tzinfo=qzone.BEIJING), [],
                                       examples=["困。", "银扣又掉了一颗。"])
    assert "困。" in system and "银扣又掉了一颗。" in system and "书房的窗户" not in system


def test_qzone_history_with_dates(monkeypatch, tmp_path):
    reply = json.dumps({"text": LONG_POST, "mood": "还行", "scene": "门口"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply])
    now = datetime(2026, 10, 28, 18, tzinfo=qzone.BEIJING)
    qzone.remember("新鞋磨脚。", now - timedelta(days=6))
    qzone.remember("床单晾不干。", now - timedelta(days=1))
    qzone.remember("困。", now - timedelta(hours=3))
    # 旧版本没存时间的也照样能用
    path = tmp_path / "qzone_history.json"
    path.write_text(json.dumps([{"text": "老的一条"}, *json.loads(path.read_text(encoding="utf-8"))],
                               ensure_ascii=False), encoding="utf-8")
    asyncio.run(qzone.write_post(now=now))
    system = calls[0][0]["content"]
    assert "10月22日（6 天前）：新鞋磨脚。" in system
    assert "10月27日（昨天）：床单晾不干。" in system
    assert "10月28日（今天）：困。" in system
    assert "- 老的一条" in system
    assert "接着之前说过的事" in system


def test_qzone_day_notes():
    from datetime import date

    from mytest.plugins.tools_api_plugin.qzone import day_notes

    assert day_notes(date(2026, 10, 20)) == []
    assert day_notes(date(2026, 10, 28)) == ["大后天是万圣夜"]
    assert day_notes(date(2026, 10, 31)) == ["周末", "今天是万圣夜", "月底"]
    assert day_notes(date(2026, 12, 23)) == ["明天是平安夜"]
    assert day_notes(date(2026, 12, 25)) == ["今天是圣诞节"]
    assert day_notes(date(2027, 2, 6)) == ["周末", "今天是春节"]
    assert day_notes(date(2027, 2, 5)) == ["今天是除夕"]
    assert day_notes(date(2027, 2, 2)) == ["大后天是除夕", "月初"]
    assert "今天是七夕" in day_notes(date(2027, 8, 8))
    # 中秋和国庆假期撞在一天时说中秋
    assert "今天是中秋节" in day_notes(date(2028, 10, 3))
    assert day_notes(date(2026, 11, 2)) == ["月初"]
    assert day_notes(date(2026, 11, 28)) == ["周末", "月底"]
    assert day_notes(date(2026, 12, 30)) == ["明天是跨年夜", "月底"]
    # 农历表之外的年份只算公历节日
    assert day_notes(date(2040, 12, 25)) == ["今天是圣诞节"]


def test_qzone_user_prompt_has_day_notes(monkeypatch, tmp_path):
    reply = json.dumps({"text": LONG_POST, "mood": "还行", "scene": "厨房"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply])
    asyncio.run(qzone.write_post(slot="work", now=datetime(2026, 12, 25, 10, tzinfo=qzone.BEIJING)))
    first_line = calls[0][1]["content"].splitlines()[0]
    assert first_line == "现在是12月25日，星期五，10点，冬天，今天是圣诞节。"


def test_qzone_tool_superuser_only_and_returns_image(sent, monkeypatch):
    import base64

    import mytest.plugins.tools_api_plugin.tools as tools
    from mytest.plugins.tools_api_plugin import qzone, selfie

    monkeypatch.setattr(tools, "_is_superuser", lambda user_id: user_id == "1196477724")
    monkeypatch.setattr(qzone, "qzone_ready", lambda: "")
    monkeypatch.setattr(selfie, "selfie_ready", lambda: "")
    # 画图关掉时全是自拍
    monkeypatch.setattr(tools, "_photo_ready", lambda: "画图功能已关闭")
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

    with pytest.raises(ToolError, match="superuser"):
        asyncio.run(tools.TOOLS["qzone"].run(ToolContext("123456", "654321"), {}))
    with pytest.raises(ToolError, match="superuser"):
        asyncio.run(tools.TOOLS["qzone"].run(ToolContext("123456", None), {"slot": "work"}))

    owner = ToolContext("1196477724", "654321")
    assert asyncio.run(tools.TOOLS["qzone"].run(owner, {"check_only": True}))["content"] == "可以发"
    assert scenes == [] and remembered == []

    result = asyncio.run(tools.TOOLS["qzone"].run(owner, {"mood": "心情" * 20, "topic": "下雪"}))
    assert result["text"] == "停电了。" and base64.b64decode(result["image"]) == b"png"
    assert asked == {"topic": "下雪", "mood": ("心情" * 20)[:20], "slot": "", "photo": qzone.SELFIE}
    assert result["photo"] == qzone.SELFIE
    assert scenes == ["烛光下。表情和气氛：有点烦"] and remembered == ["停电了。"]
    asyncio.run(tools.TOOLS["qzone"].run(ToolContext("1196477724", None), {"slot": "bedtime"}))
    assert asked["slot"] == "bedtime"
    assert sent == [], "说说不发到聊天里"

    monkeypatch.setattr(qzone, "qzone_ready", lambda: "发说说功能已关闭")
    with pytest.raises(ToolError, match="关闭"):
        asyncio.run(tools.TOOLS["qzone"].run(owner, {"check_only": True}))


def test_qzone_tool_photo_path(sent, monkeypatch):
    import base64

    import mytest.plugins.tools_api_plugin.tools as tools
    from mytest.plugins.tools_api_plugin import qzone, selfie

    monkeypatch.setattr(tools, "_is_superuser", lambda user_id: True)
    monkeypatch.setattr(qzone, "qzone_ready", lambda: "")
    monkeypatch.setattr(qzone, "remember", lambda text, now=None: None)
    monkeypatch.setattr(selfie, "selfie_ready", lambda: "")
    monkeypatch.setattr(tools, "_photo_ready", lambda: "")
    monkeypatch.setattr(qzone, "pick_photo", lambda rng, **kwargs: qzone.PHOTO)
    prompts, asked = [], {}

    async def fake_write(**kwargs):
        asked.update(kwargs)
        return qzone.Post(text="茶凉了。", mood="发呆", scene="窗台上一杯凉掉的红茶", photo=kwargs["photo"])

    async def fake_photo(prompt):
        prompts.append(prompt)
        return b"jpg"

    async def no_selfie(scene):
        raise AssertionError("拍东西时不画自拍")

    monkeypatch.setattr(qzone, "write_post", fake_write)
    monkeypatch.setattr(tools, "_take_photo", fake_photo)
    monkeypatch.setattr(selfie, "take_selfie", no_selfie)

    owner = ToolContext("1196477724", None)
    result = asyncio.run(tools.TOOLS["qzone"].run(owner, {"mood": "发呆"}))
    assert result["photo"] == qzone.PHOTO and base64.b64decode(result["image"]) == b"jpg"
    assert asked["photo"] == qzone.PHOTO
    assert prompts == ["随手用手机拍的照片，画面里没有人：窗台上一杯凉掉的红茶。气氛：发呆"]

    # 两种图都画不了：检查阶段就拒绝
    monkeypatch.setattr(selfie, "selfie_ready", lambda: "还没有上传人设图")
    monkeypatch.setattr(tools, "_photo_ready", lambda: "画图功能已关闭")
    with pytest.raises(ToolError, match="配不了图"):
        asyncio.run(tools.TOOLS["qzone"].run(owner, {"check_only": True}))
    assert sent == []


def test_qzone_pick_photo(monkeypatch, tmp_path):
    qzone, _ = _qzone_env(monkeypatch, tmp_path, [])
    rng = random.Random(0)
    assert qzone.pick_photo(rng, can_selfie=True, can_photo=False) == qzone.SELFIE
    assert qzone.pick_photo(rng, can_selfie=False, can_photo=True) == qzone.PHOTO
    assert qzone.pick_photo(rng, can_selfie=False, can_photo=False) == ""
    picks = [qzone.pick_photo(rng, can_selfie=True, can_photo=True) for _ in range(1000)]
    assert 250 < picks.count(qzone.PHOTO) < 450, "默认 35% 拍东西"
    qzone.qzone_settings.photo_ratio = 0
    assert {qzone.pick_photo(rng, can_selfie=True, can_photo=True) for _ in range(100)} == {qzone.SELFIE}
    qzone.qzone_settings.photo_ratio = 100
    assert {qzone.pick_photo(rng, can_selfie=True, can_photo=True) for _ in range(100)} == {qzone.PHOTO}


def test_qzone_photo_prompts(monkeypatch, tmp_path):
    reply = json.dumps({"text": LONG_POST, "mood": "困", "scene": "床头的小灯"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply] * 2)
    night = datetime(2026, 10, 28, 23, tzinfo=qzone.BEIJING)

    post = asyncio.run(qzone.write_post(slot="bedtime", now=night, photo=qzone.PHOTO))
    assert post.photo == qzone.PHOTO
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert qzone.PHOTO_HINT in user and "睡衣" not in user, "拍东西时不写穿着"
    assert qzone.SCENE_FORMATS[qzone.PHOTO] in system and qzone.SCENE_FORMATS[qzone.SELFIE] not in system

    post = asyncio.run(qzone.write_post(slot="bedtime", now=night))
    assert post.photo == qzone.SELFIE
    system, user = calls[1][0]["content"], calls[1][1]["content"]
    assert qzone.PHOTO_HINT not in user and "睡衣" in user
    assert qzone.SCENE_FORMATS[qzone.SELFIE] in system


def test_qzone_selfie_outfit_rotates(monkeypatch, tmp_path):
    import random

    reply = json.dumps({"text": LONG_POST, "mood": "累", "scene": "沙发"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply] * 20)
    evening = datetime(2026, 10, 28, 18, tzinfo=qzone.BEIJING)
    rng = random.Random(1)
    for _ in range(20):
        asyncio.run(qzone.write_post(slot="off_work", now=evening, rng=rng))
    users = [c[1]["content"] for c in calls]
    picked = {o for o in qzone.SLOTS["off_work"].outfits for u in users if o in u}
    assert len(picked) > 1, "每次从时段的几种穿着里抽一种"
    assert all(qzone.SELFIE_HINT in u for u in users)


def test_qzone_user_prompt_has_weather(monkeypatch, tmp_path):
    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.tools_api_plugin import weather

    reply = json.dumps({"text": LONG_POST, "mood": "还行", "scene": "窗边"}, ensure_ascii=False)
    qzone, calls = _qzone_env(monkeypatch, tmp_path, [reply] * 3)
    asked = []

    async def fake_fetch(city):
        asked.append(city)
        return "杭州现在下小雨，16°C"

    monkeypatch.setattr(weather, "fetch", fake_fetch)
    # 没填城市不查
    asyncio.run(qzone.write_post())
    assert asked == [] and "天气" not in calls[0][1]["content"]

    qzone.qzone_settings.city = "杭州"
    asyncio.run(qzone.write_post())
    assert asked == ["杭州"] and "她那边的天气：杭州现在下小雨，16°C。" in calls[1][1]["content"]

    # 查不到也照常写，只是不带天气
    async def broken(city):
        raise ServiceError("找不到")

    monkeypatch.setattr(weather, "fetch", broken)
    asyncio.run(qzone.write_post())
    assert "天气" not in calls[2][1]["content"]


def test_weather_describe():
    from mytest.plugins.tools_api_plugin.weather import describe

    data = {
        "current": {"temperature_2m": 16.0, "apparent_temperature": 15.8, "weather_code": 51, "wind_speed_10m": 6.2},
        "daily": {"temperature_2m_max": [22.6], "temperature_2m_min": [15.2], "precipitation_probability_max": [75]},
    }
    assert describe("上海", data) == "上海现在下毛毛雨，16°C，今天 15 到 23°C，降水概率 75%"
    cold = {"current": {"temperature_2m": 2.0, "apparent_temperature": -3.4, "weather_code": 71, "wind_speed_10m": 35}}
    assert describe("北京", cold) == "北京现在下小雪，2°C（体感 -3°C），风很大"
    # 字段缺了也不出错
    assert describe("杭州", {}) == "杭州现在"
    assert describe("杭州", {"current": {"weather_code": 1234}, "daily": {"temperature_2m_max": []}}) == "杭州现在"


def test_weather_locate_and_cache(monkeypatch):
    from mytest.plugins._shared.services.http import ServiceError
    from mytest.plugins.tools_api_plugin import weather

    monkeypatch.setattr(weather, "_places", {})
    monkeypatch.setattr(weather, "_cache", {})
    requests = []

    async def fake_get_json(url, params):
        requests.append((url, params.get("name")))
        if url == weather.FORECAST_URL:
            return {"current": {"temperature_2m": 20, "weather_code": 0}}
        if params["name"] == "杭州":
            return {"results": [{"latitude": 30.29, "longitude": 120.16, "name": "杭州"}]}
        return {"generationtime_ms": 0.1}

    monkeypatch.setattr(weather, "_get_json", fake_get_json)
    # 「杭州市」查不到，去掉「市」再查
    assert asyncio.run(weather.locate("杭州市")) == (30.29, 120.16, "杭州")
    assert [name for _, name in requests] == ["杭州市", "杭州"]
    with pytest.raises(ServiceError, match="找不到"):
        asyncio.run(weather.locate("不存在的地方"))

    requests.clear()
    assert asyncio.run(weather.fetch("杭州市")) == "杭州现在晴，20°C"
    assert asyncio.run(weather.fetch("杭州市")) == "杭州现在晴，20°C"
    # 城市坐标记住了，天气半小时内也不重复查
    assert requests == [(weather.FORECAST_URL, None)]

    assert asyncio.run(weather.current_weather("  ")) == ""
    assert asyncio.run(weather.current_weather("不存在的地方")) == ""


def test_qzone_weather_tester(monkeypatch, tmp_path):
    from mytest.plugins.tools_api_plugin import weather

    qzone, _ = _qzone_env(monkeypatch, tmp_path, [])

    async def fake_fetch(city):
        return f"{city}现在晴"

    monkeypatch.setattr(weather, "fetch", fake_fetch)
    assert "没有填城市" in asyncio.run(qzone.test_weather())
    qzone.qzone_settings.city = "杭州"
    assert asyncio.run(qzone.test_weather()) == "天气查到了：杭州现在晴"


def test_rinne_tools_plan_slots():
    from datetime import date, datetime

    mod = _load_rinne_tools()
    day = date(2026, 10, 28)
    rng = random.Random(1)
    cfg = mod.QzoneSection()
    assert mod.slot_times(cfg) == {"work": "10:00", "off_work": "18:00", "bedtime": "23:00"}
    for _ in range(50):
        slots = mod.plan_slots(day, mod.slot_times(cfg), 40, rng)
        assert [s for _, s in slots] == ["work", "off_work", "bedtime"]
        for at, slot in slots:
            h = {"work": 10, "off_work": 18, "bedtime": 23}[slot]
            base = datetime(2026, 10, 28, h, tzinfo=mod.BEIJING)
            assert abs((at - base).total_seconds()) <= 40 * 60
    # 留空或写错的时段不发
    slots = mod.plan_slots(day, {"work": "", "off_work": "25:00", "bedtime": "9:61"}, 40, rng)
    assert slots == []
    exact = mod.plan_slots(day, {"work": "08:00"}, 0, rng)
    assert exact == [(datetime(2026, 10, 28, 8, tzinfo=mod.BEIJING), "work")]


def test_rinne_tools_scheduled_post_rolls_at_slot(monkeypatch):
    mod = _load_rinne_tools()
    plugin, logs, _ = _tools_plugin(mod, {})
    cfg = mod.RinneToolsConfig()
    cfg.qzone.superuser_id = "1196477724"
    plugin._cfg = lambda: cfg
    posted = []

    async def fake_post(body):
        posted.append(body)
        return "ok"

    monkeypatch.setattr(plugin, "_post_qzone", fake_post)
    cfg.qzone.probability = 0.0
    asyncio.run(plugin._scheduled_post("work", random.Random(0)))
    assert posted == [] and any("不发" in line for line in logs)
    cfg.qzone.probability = 1.0
    asyncio.run(plugin._scheduled_post("bedtime", random.Random(0)))
    assert posted == [{"user_id": "1196477724", "group_id": None, "args": {"slot": "bedtime"}}]


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
        cfg.qzone.superuser_id = owner_id
        plugin._cfg = lambda: cfg
        plugin._restart_scheduler()
        task = plugin._scheduler_task
        if task is not None:
            task.cancel()
        return task, logs

    task, logs = asyncio.run(run(""))
    assert task is None and any("superuser_id" in line for line in logs)
    task, _ = asyncio.run(run("1196477724"))
    assert task is not None
