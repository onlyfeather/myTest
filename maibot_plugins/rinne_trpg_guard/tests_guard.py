"""跑团群静默插件的本地测试。

需要先安装插件 SDK：pip install maibot-plugin-sdk
运行：python -m pytest maibot_plugins/rinne_trpg_guard/tests_guard.py -q

消息结构照搬自探测插件在真实 MaiBot 1.3.15 上记录的数据（和好感度插件的测试一样）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from maibot_sdk.components import collect_components

PLUGIN_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rinne_trpg_guard_plugin", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def inbound(group: str | None, text: str = "我推开门") -> dict:
    info = {"user_info": {"user_id": "10001", "user_nickname": "nick"}}
    if group:
        info["group_info"] = {"group_id": group, "group_name": "跑团群"}
    return {"message_id": "1", "platform": "qq", "message_info": info, "processed_plain_text": text}


def make_plugin(mode: str, groups: list[str]):
    # 和好感度插件的测试一样：ctx 是 SDK 运行时注入的只读属性，这里换成假的
    plugin = mod.create_plugin()
    tmp = tempfile.mkdtemp()
    ctx = SimpleNamespace(logger=logging.getLogger("guard-test"), paths=SimpleNamespace(data_dir=tmp))
    type(plugin).ctx = property(lambda self: self.__dict__["_fake_ctx"])
    plugin.__dict__["_fake_ctx"] = ctx
    plugin.set_plugin_config({"plugin": {"config_version": "1.0.0", "mode": mode, "groups": groups}})
    asyncio.run(plugin.on_load())
    return plugin, Path(tmp) / "guard.jsonl"


def test_group_detection():
    assert mod.group_of({"message": inbound("123")}) == "123"
    assert mod.group_of({"message": {"data": inbound("456")}}) == "456", "wrapped message"
    assert mod.group_of({"message": inbound(None)}) == "", "private chat"
    assert mod.group_of({"message": "oops"}) == ""
    assert mod.should_block({"message": inbound("123")}, ["123", " 999 "]) == "123"
    assert mod.should_block({"message": inbound("999")}, [" 999 "]) == "999", "spaces in config"
    assert mod.should_block({"message": inbound("555")}, ["123"]) == ""
    assert mod.should_block({"message": inbound("123")}, []) == "", "nothing configured, nothing blocked"


def test_observe_mode_records_but_never_blocks():
    plugin, log = make_plugin("observe", ["123"])
    assert asyncio.run(plugin.guard(message=inbound("123"))) == {"action": "continue"}
    assert asyncio.run(plugin.guard(message=inbound("555"))) == {"action": "continue"}
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"blocked": false' in lines[0] and "我推开门" in lines[0]


def test_block_mode_only_blocks_listed_groups():
    plugin, log = make_plugin("block", ["123"])
    assert asyncio.run(plugin.guard(message=inbound("123"))) == {"action": "abort"}
    assert asyncio.run(plugin.guard(message=inbound("555"))) == {"action": "continue"}
    assert asyncio.run(plugin.guard(message=inbound(None))) == {"action": "continue"}, "private chats untouched"
    assert '"blocked": true' in log.read_text(encoding="utf-8")


def test_errors_let_the_message_through(monkeypatch):
    plugin, _ = make_plugin("block", ["123"])

    def boom(*args, **kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr(mod, "should_block", boom)
    assert asyncio.run(plugin.guard(message=inbound("123"))) == {"action": "continue"}


def test_manifest_and_hook_declaration():
    import json
    import re

    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", manifest["id"])
    plugin, _ = make_plugin("observe", [])
    hooks = [c for c in collect_components(plugin) if c["type"] == "HOOK_HANDLER"]
    assert len(hooks) == 1
    meta = hooks[0]["metadata"]
    assert (meta["hook"], meta["mode"], meta["order"], meta["error_policy"]) == (
        "chat.receive.before_process", "blocking", "early", "skip")


def test_default_config_is_safe():
    plugin, _ = make_plugin("observe", [])
    assert mod.GeneralSection().mode == "observe", "first deployment never blocks anything"
    assert asyncio.run(plugin.guard(message=inbound("123"))) == {"action": "continue"}
