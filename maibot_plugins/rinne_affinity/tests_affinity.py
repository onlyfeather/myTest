"""凛音好感度插件的本地测试。

需要先安装插件 SDK：pip install maibot-plugin-sdk
运行：python maibot_plugins/rinne_affinity/tests_affinity.py

Hook 收到的参数结构照搬自探测插件在真实 MaiBot 1.3.15 上记录的数据。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from maibot_sdk.components import collect_components
from maibot_sdk.config import build_plugin_default_config, extract_plugin_config_version

PLUGIN_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rinne_affinity_plugin", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BOT = "3974127672"
GROUP_SESSION = "f245790b02131115471e536327ee56ec"


def inbound(message_id, user_id, text, *, group=True, card="千树与梦"):
    info = {"user_info": {"user_id": user_id, "user_nickname": "nick", "user_cardname": card}}
    if group:
        info["group_info"] = {"group_id": "123456", "group_name": "测试群"}
    return {
        "message_id": message_id,
        "platform": "qq",
        "account_id": BOT,
        "message_info": info,
        "processed_plain_text": text,
        "session_id": GROUP_SESSION if group else "private-session",
    }


def before_request_kwargs(reply_id, session=GROUP_SESSION):
    return {
        "hook_name": "maisaka.replyer.before_request",
        "session_id": session,
        "request_type": "maisaka.replyer",
        "task_name": "replyer",
        "model_name": "",
        "extra_prompt": "",
        "attempt": 1,
        "retry_count": 0,
        "max_retries": 3,
        "reply_message_id": reply_id,
        "reply_reason": "",
        "selected_expression_ids": [],
        "reply_tool_args": {"reply_reference": "...", "reply_style": "简短表达"},
    }


class FakeLLM:
    def __init__(self):
        self.responses = []
        self.calls = []

    async def generate(self, prompt, model="", temperature=None, max_tokens=None):
        self.calls.append(prompt)
        text = self.responses.pop(0) if self.responses else '{"delta": 0, "reason": "普通"}'
        return {"success": True, "response": text, "model": "fake"}


class FakeMessage:
    def __init__(self):
        self.store = {}
        self.calls = 0

    async def get_by_id(self, message_id, chat_id="", stream_id=""):
        self.calls += 1
        if message_id not in self.store:
            raise LookupError("not found")
        return self.store[message_id]


class FakeSend:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


def make_plugin(config=None):
    plugin = mod.create_plugin()
    ctx = SimpleNamespace(
        logger=logging.getLogger("affinity-test"),
        paths=SimpleNamespace(data_dir=Path(tempfile.mkdtemp())),
        llm=FakeLLM(),
        message=FakeMessage(),
        send=FakeSend(),
    )
    type(plugin).ctx = property(lambda self: self.__dict__["_fake_ctx"])
    plugin.__dict__["_fake_ctx"] = ctx
    plugin.set_plugin_config(config or {})
    return plugin, ctx


async def drain(plugin):
    if plugin._tasks:
        await asyncio.gather(*list(plugin._tasks))


def test_manifest_and_components():
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", manifest["id"])
    assert set(manifest["capabilities"]) == {"send.text", "message.get_by_id", "llm.generate"}
    plugin, _ = make_plugin()
    components = collect_components(plugin)
    hooks = sorted(c["metadata"]["hook"] for c in components if c["type"] == "HOOK_HANDLER")
    commands = sorted(c["name"] for c in components if c["type"] == "COMMAND")
    assert hooks == ["chat.receive.after_process", "maisaka.replyer.after_response",
                     "maisaka.replyer.before_request"], hooks
    assert commands == ["affinity_set", "affinity_view"], commands


def test_config_defaults():
    default = build_plugin_default_config(mod.RinneAffinityConfig)
    assert extract_plugin_config_version(default) == "1.0.0"
    assert default["plugin"]["default_favorability"] == 50
    assert default["scoring"]["max_delta"] == 3


def test_rules():
    rules = mod.DEFAULT_RULES
    noon = datetime(2026, 1, 1, 12, tzinfo=mod.BEIJING)
    night = datetime(2026, 1, 1, 23, tzinfo=mod.BEIJING)
    early = datetime(2026, 1, 1, 3, tzinfo=mod.BEIJING)

    assert mod.stage_for(rules, 94)["name"] == "我的主人"
    assert mod.stage_for(rules, 95)["name"] == "唯一的主人"
    assert not mod.duty_state(rules, True, 100, night).off_duty, "group is always on duty"
    assert not mod.duty_state(rules, False, 94, noon).off_duty
    assert mod.duty_state(rules, False, 95, noon).off_duty
    assert mod.duty_state(rules, False, 60, night).off_duty
    assert mod.duty_state(rules, False, 60, early).off_duty
    assert not mod.duty_state(rules, False, 59, night).off_duty

    top = mod.stage_for(rules, 95)
    assert "呆货" in mod.user_call_for(top, mod.duty_state(rules, False, 95, noon))
    assert mod.user_call_for(top, mod.duty_state(rules, True, 95, noon)) == "主人"
    assert "呆货" not in mod.user_call_for(mod.stage_for(rules, 94), mod.duty_state(rules, False, 94, night))


def test_rules_file_can_be_nonebot_personas():
    personas = PLUGIN_DIR.parent.parent / "mytest" / "plugins" / "dice_girl_plugin" / "personas.json"
    rules, source = mod.load_rules(str(personas), "rinne")
    assert source == str(personas)
    assert mod.stage_for(rules, 95)["name"] == mod.stage_for(mod.DEFAULT_RULES, 95)["name"]
    assert rules["duty"]["off_duty_min_favorability"] == mod.DEFAULT_RULES["duty"]["off_duty_min_favorability"]


def test_parse_delta():
    assert mod.parse_delta('{"delta": 2, "reason": "关心"}', 3) == (2, "关心")
    assert mod.parse_delta('好的\n```json\n{"delta": 9, "reason": "x"}\n```', 3) == (3, "x")
    assert mod.parse_delta('{"delta": -9}', 3) == (-3, "")
    assert mod.parse_delta("没有 json", 3) == (0, "无法解析")
    assert mod.parse_delta('{"delta": "abc"}', 3) == (0, "无法解析")


def test_extract_sender_shapes():
    s = mod.extract_sender(inbound("1", "42", "hi"))
    assert (s.user_id, s.name, s.is_group, s.is_bot) == ("42", "千树与梦", True, False)
    p = mod.extract_sender(inbound("2", "42", "hi", group=False, card=None))
    assert p.is_group is False and p.name == "nick"
    wrapped = mod.extract_sender({"message": inbound("3", "42", "hi")})
    assert wrapped.user_id == "42"
    assert mod.extract_sender({"foo": 1}) is None
    assert mod.extract_sender(inbound("4", BOT, "我自己")).is_bot


async def _test_inject_and_score_group():
    plugin, ctx = make_plugin()
    await plugin.on_load()
    await plugin.remember_sender(hook_name="chat.receive.after_process", message=inbound("m1", "42", "你还记得我们第一次相遇吗"))

    kwargs = before_request_kwargs("m1")
    result = await plugin.inject_relationship(**kwargs)
    extra = result["modified_kwargs"]["extra_prompt"]
    assert mod.PROMPT_MARKER in extra and "服务对象" in extra and "工作中" in extra
    assert "称呼对方：您" in extra
    assert "呆货" in extra and "绝不使用" in extra, "on-duty rule forbids 呆货"
    # 其余参数原样保留
    assert result["modified_kwargs"]["reply_tool_args"] == kwargs["reply_tool_args"]

    # 重试时已经带着标记，不重复追加
    retry = await plugin.inject_relationship(**{**kwargs, "extra_prompt": extra, "attempt": 2})
    assert "modified_kwargs" not in retry

    ctx.llm.responses = ['{"delta": 2, "reason": "真诚的问候"}']
    await plugin.schedule_scoring(reply_message_id="m1", response="恕我直言，这个群刚建立不久……", session_id=GROUP_SESSION)
    await plugin.schedule_scoring(reply_message_id="m1", response="重试的回复", session_id=GROUP_SESSION)
    await drain(plugin)
    assert len(ctx.llm.calls) == 1, "only score once per reply"
    assert "你还记得我们第一次相遇吗" in ctx.llm.calls[0][1]["content"]
    assert plugin._store.get("42", 50) == 52


async def _test_private_off_duty_and_fallback_lookup():
    plugin, ctx = make_plugin()
    await plugin.on_load()
    plugin._store.change("42", default=50, value=96, source="test")
    # 缓存里没有这条消息，走 message.get_by_id 兜底
    ctx.message.store["p1"] = {"message": inbound("p1", "42", "今天好累", group=False)}
    result = await plugin.inject_relationship(**before_request_kwargs("p1", session="private-session"))
    extra = result["modified_kwargs"]["extra_prompt"]
    assert ctx.message.calls == 1
    assert "唯一的主人" in extra and "下班后" in extra and "呆货" in extra and "绝不使用" not in extra


async def _test_skips():
    plugin, ctx = make_plugin()
    await plugin.on_load()
    # 找不到对象：不改动
    result = await plugin.inject_relationship(**before_request_kwargs("unknown"))
    assert "modified_kwargs" not in result
    # 回复的是机器人自己的消息：不改动、不评分
    await plugin.remember_sender(message=inbound("b1", BOT, "我自己说的"))
    assert "modified_kwargs" not in await plugin.inject_relationship(**before_request_kwargs("b1"))
    await plugin.schedule_scoring(reply_message_id="b1", response="x")
    await drain(plugin)
    assert ctx.llm.calls == []
    # 关闭自动评分
    plugin2, ctx2 = make_plugin({"plugin": {"config_version": "1.0.0"}, "scoring": {"enabled": False}})
    await plugin2.on_load()
    await plugin2.remember_sender(message=inbound("m9", "7", "hi"))
    await plugin2.schedule_scoring(reply_message_id="m9", response="x")
    await drain(plugin2)
    assert ctx2.llm.calls == []
    # 模型调用失败：好感度不变，不抛异常
    plugin3, ctx3 = make_plugin()
    await plugin3.on_load()

    async def broken(**kwargs):
        raise RuntimeError("model down")

    ctx3.llm.generate = broken
    await plugin3.remember_sender(message=inbound("m8", "8", "hi"))
    await plugin3.schedule_scoring(reply_message_id="m8", response="x")
    await drain(plugin3)
    assert plugin3._store.get("8", 50) == 50


async def _test_commands():
    plugin, ctx = make_plugin({"plugin": {"config_version": "1.0.0", "admin_ids": ["1001"]}})
    await plugin.on_load()
    user_msg = inbound("c1", "42", "/好感度")
    admin_msg = inbound("c2", "1001", "/设置好感度 42 120")

    await plugin.cmd_view(stream_id="s", message=user_msg, matched_groups={})
    assert "好感度：50（服务对象）" in ctx.send.sent[-1][1]

    await plugin.cmd_view(stream_id="s", message=user_msg, matched_groups={"target": "1001"})
    assert "只能查看自己" in ctx.send.sent[-1][1]

    await plugin.cmd_set(stream_id="s", message=user_msg, matched_groups={"target": "42", "value": "99"})
    assert "只有管理员" in ctx.send.sent[-1][1]

    await plugin.cmd_set(stream_id="s", message=admin_msg, matched_groups={"target": "42", "value": "120"})
    assert "从 50 设为 100（唯一的主人）" in ctx.send.sent[-1][1]

    await plugin.cmd_view(stream_id="s", message=admin_msg, matched_groups={"target": "42"})
    assert "100（唯一的主人）" in ctx.send.sent[-1][1] and "管理员 1001 手动设置" in ctx.send.sent[-1][1]

    no_admin, ctx2 = make_plugin()
    await no_admin.on_load()
    await no_admin.cmd_set(stream_id="s", message=admin_msg, matched_groups={"target": "42", "value": "1"})
    assert "还没有设置管理员" in ctx2.send.sent[-1][1]

    for pattern_owner, text, should_match in (
        (plugin.cmd_view, "/好感度", True),
        (plugin.cmd_view, "/好感度 123456", True),
        (plugin.cmd_view, "/好感度度", False),
        (plugin.cmd_set, "/设置好感度 123456 80", True),
        (plugin.cmd_set, "/设置好感度 123456", False),
    ):
        pattern = getattr(pattern_owner, "__maibot_component_info__").command_pattern
        assert bool(re.match(pattern, text)) is should_match, (pattern, text)


async def _test_preset_modes():
    """凛音工具插件先写入「【聊天预设】生效模式：…」，好感度插件据此配合。"""
    plugin, ctx = make_plugin()
    await plugin.on_load()

    # 强攻击力：不加关系信息，也不评分
    await plugin.remember_sender(message=inbound("a1", "42", "你个傻子"))
    aggressive = {**before_request_kwargs("a1"), "extra_prompt": "【聊天预设】生效模式：aggressive\n直接开喷"}
    assert "modified_kwargs" not in await plugin.inject_relationship(**aggressive)
    await plugin.schedule_scoring(reply_message_id="a1", response="滚", session_id=GROUP_SESSION)
    await drain(plugin)
    assert ctx.llm.calls == [] and plugin._store.get("42", 50) == 50

    # 两个模式都开：按强攻击力处理
    both = {**aggressive, "reply_message_id": "a2", "extra_prompt": "【聊天预设】生效模式：prefix,nsfw,aggressive\n..."}
    await plugin.remember_sender(message=inbound("a2", "42", "hi"))
    assert "modified_kwargs" not in await plugin.inject_relationship(**both)

    # NSFW：只给称呼和亲疏，不加限制亲密的阶段要求和上班规则
    await plugin.remember_sender(message=inbound("n1", "42", "抱一下"))
    nsfw = {**before_request_kwargs("n1"), "extra_prompt": "【聊天预设】生效模式：prefix,nsfw\n成人模式"}
    extra = (await plugin.inject_relationship(**nsfw))["modified_kwargs"]["extra_prompt"]
    assert extra.startswith("【聊天预设】生效模式：prefix,nsfw") and mod.PROMPT_MARKER in extra
    assert "称呼对方：您" in extra and "成人模式开着" in extra
    assert "被靠近时礼貌地后退一步" not in extra and "全程使用敬语" not in extra and "绝不使用" not in extra
    ctx.llm.responses = ['{"delta": 1, "reason": "真诚"}']
    await plugin.schedule_scoring(reply_message_id="n1", response="……过来。", session_id=GROUP_SESSION)
    await drain(plugin)
    assert "成人模式" in ctx.llm.calls[0][0]["content"], "scoring is told not to punish flirting"
    assert plugin._store.get("42", 50) == 51

    # 没有预设：和原来一样
    await plugin.remember_sender(message=inbound("p1", "42", "hi"))
    plain = (await plugin.inject_relationship(**before_request_kwargs("p1")))["modified_kwargs"]["extra_prompt"]
    assert "全程使用敬语" in plain and "成人模式开着" not in plain
    assert mod.preset_modes("【聊天预设】\n旧格式") == set()


def test_async_flows():
    for coro in (_test_inject_and_score_group, _test_private_off_duty_and_fallback_lookup, _test_skips, _test_commands,
                 _test_preset_modes):
        asyncio.run(coro())


def run() -> None:
    for name, func in list(globals().items()):
        if name.startswith("test_"):
            func()
    print("rinne affinity plugin tests passed")


if __name__ == "__main__":
    run()
