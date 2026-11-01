"""「不接指令」插件的本地测试。

需要先安装插件 SDK：pip install maibot-plugin-sdk
运行：python -m pytest maibot_plugins/rinne_command_guard/tests_guard.py -q

消息结构按 MaiBot 的 PluginMessageUtils._session_message_to_dict：raw_message 是消息段列表。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.components import collect_components

PLUGIN_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rinne_command_guard_plugin", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def inbound(*segments, group: str | None = "123", user: str = "10001") -> dict:
    raw = [{"type": "text", "data": s} if isinstance(s, str) else s for s in segments]
    info = {"user_info": {"user_id": user, "user_nickname": "nick"}}
    if group:
        info["group_info"] = {"group_id": group, "group_name": "群"}
    return {"message_id": "1", "platform": "qq", "message_info": info, "raw_message": raw}


REPLY = {"type": "reply", "data": {"target_message_id": "9", "target_message_content": "/画图 猫"}}
AT = {"type": "at", "data": {"target_user_id": "3974127672", "target_user_nickname": "白鸟凛音"}}
IMAGE = {"type": "image", "data": "", "hash": "abc"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_plugin(mode: str, **overrides):
    plugin = mod.create_plugin()
    tmp = tempfile.mkdtemp()
    ctx = SimpleNamespace(logger=logging.getLogger("command-guard-test"), paths=SimpleNamespace(data_dir=tmp))
    type(plugin).ctx = property(lambda self: self.__dict__["_fake_ctx"])
    plugin.__dict__["_fake_ctx"] = ctx
    plugin.set_plugin_config({"plugin": {"config_version": "1.0.0", "mode": mode, **overrides}})
    asyncio.run(plugin.on_load())
    return plugin, Path(tmp) / "skipped.jsonl"


def cmd(text: str) -> bool:
    return mod.is_command(text, mod.DEFAULT_PATTERNS, mod.DEFAULT_KEEP_PREFIXES)


@pytest.mark.parametrize("text", [
    "/画图 海边的白发少女", "/nai -参考 猫", "/gpt画图 -多图 图一的人", "/点歌 晴天", "/说 你好",
    "/帮助", "/再画", "/改图 换成夜晚", "/p站 每日一图", "/dice 投骰", "／画图 全角斜杠",
    ".rd", ".ra 侦查", "。rd3d6", ".st 力量50", ".sc 1/1d6",
    "搜图 初音", "p站 搜图 初音", "最新", "热门 原神", "美图", "每日一图", "p站 关注列表",
])
def test_nonebot_commands_are_recognised(text):
    assert cmd(text)


@pytest.mark.parametrize("text", [
    "", "你好", "画图好难", "我想搜图片", "最新的消息看了吗", "每日一图呢", "。。。", "...", ". 好",
    "1/2", "http://x.com", "/ 好", "/好感度", "/好感度 12345678", "/设置好感度 123456 50", "/clear",
])
def test_normal_chat_and_maibot_commands_pass(text):
    assert not cmd(text)


def test_text_ignores_reply_at_and_images():
    assert mod.text_of(inbound(REPLY, AT, " /画图 猫")) == "/画图 猫"
    assert mod.text_of(inbound("@白鸟凛音 /点歌 晴天")) == "/点歌 晴天", "@ typed as text"
    assert mod.text_of(inbound(IMAGE)) == ""
    assert mod.text_of({"message_info": {}, "processed_plain_text": " .rd "}) == ".rd", "no raw_message"


def test_chat_key():
    assert mod.chat_key(inbound("x", group="123", user="1")) == ("group:123", "1")
    assert mod.chat_key(inbound("x", group=None, user="1")) == ("private:1", "1")
    assert mod.chat_key({"message_info": {}}) is None


def test_follow_up_messages_from_same_person_same_chat():
    clock = Clock()
    follow = mod.FollowUps(clock)
    cfg = mod.GeneralSection()
    assert mod.decide({"message": inbound("/gpt画图 -多图 图一的人", user="1")}, cfg, follow)[0] == "指令"
    assert mod.decide({"message": inbound(IMAGE, user="1")}, cfg, follow)[0] == "指令后续"
    assert mod.decide({"message": inbound("好了", user="1")}, cfg, follow)[0] == "指令后续"
    assert mod.decide({"message": inbound("好了", user="2")}, cfg, follow)[0] == "", "other people unaffected"
    assert mod.decide({"message": inbound("好了", user="1", group="999")}, cfg, follow)[0] == "", "other chats"
    assert mod.decide({"message": inbound("/好感度", user="1")}, cfg, follow)[0] == "", "MaiBot commands pass"
    clock.now += 121
    assert mod.decide({"message": inbound("好了", user="1")}, cfg, follow)[0] == "", "window expired"


def test_follow_up_can_be_turned_off():
    follow = mod.FollowUps(Clock())
    cfg = mod.GeneralSection(follow_up_seconds=0)
    assert mod.decide({"message": inbound("/点歌 晴天")}, cfg, follow)[0] == "指令"
    assert mod.decide({"message": inbound("2")}, cfg, follow)[0] == ""


def test_bad_pattern_is_skipped_not_fatal():
    assert mod.is_command("/画图", ["([", r"^/\S"], [])
    assert not mod.is_command("你好", ["(["], [])


def test_observe_mode_records_but_never_blocks():
    plugin, log = make_plugin("observe")
    assert asyncio.run(plugin.guard(message=inbound("/画图 猫"))) == {"action": "continue"}
    assert asyncio.run(plugin.guard(message=inbound("你好", user="2"))) == {"action": "continue"}
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"blocked": false' in lines[0] and "/画图 猫" in lines[0]


def test_block_mode():
    plugin, log = make_plugin("block")
    assert asyncio.run(plugin.guard(message=inbound(REPLY, "/改图 换成夜晚"))) == {"action": "abort"}
    assert asyncio.run(plugin.guard(message=inbound(IMAGE))) == {"action": "abort"}, "follow-up"
    assert asyncio.run(plugin.guard(message=inbound("你好", user="2"))) == {"action": "continue"}
    assert asyncio.run(plugin.guard(message=inbound("/好感度", user="3"))) == {"action": "continue"}
    assert '"reason": "指令后续"' in log.read_text(encoding="utf-8")


def test_errors_let_the_message_through(monkeypatch):
    plugin, _ = make_plugin("block")

    def boom(*args, **kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr(mod, "decide", boom)
    assert asyncio.run(plugin.guard(message=inbound("/画图 猫"))) == {"action": "continue"}


def test_manifest_and_hook_declaration():
    import json
    import re

    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", manifest["id"])
    plugin, _ = make_plugin("observe")
    hooks = [c for c in collect_components(plugin) if c["type"] == "HOOK_HANDLER"]
    assert len(hooks) == 1
    meta = hooks[0]["metadata"]
    assert (meta["hook"], meta["mode"], meta["order"], meta["error_policy"]) == (
        "chat.receive.before_process", "blocking", "early", "skip")


def test_default_config_is_safe():
    assert mod.GeneralSection().mode == "observe", "first deployment never blocks anything"
