"""聊天预设：酒馆预设导入、宏、生效范围、WebUI 接口、MaiBot 回复前注入。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mytest.plugins._shared.presets.core import (
    MODES,
    PresetError,
    PresetStore,
    active_modes,
    build_prompt,
    clean_entries,
    expand_macros,
    parse_st_preset,
    render,
)
from mytest.plugins._shared.settings.registry import SettingsStore
from mytest.plugins.webui_plugin.server import LoginGuard, create_router

ST_PRESET = {
    "temperature": 1,
    "prompts": [
        {"identifier": "main", "name": "Main Prompt", "system_prompt": True, "role": "system",
         "content": "你是{{char}}，正在和{{user}}聊天。"},
        {"identifier": "chatHistory", "name": "Chat History", "marker": True},
        {"identifier": "charDescription", "name": "Char Description", "marker": True},
        {"identifier": "style", "name": "文风", "role": "system", "content": "{{// 作者备注}}说话短一点。"},
        {"identifier": "off", "name": "关掉的", "role": "user", "content": "不应该出现"},
        {"identifier": "orphan", "name": "不在顺序里", "content": "也不应该出现"},
        {"identifier": "empty", "name": "空的", "content": "  "},
    ],
    "prompt_order": [
        {"character_id": 100000, "order": [{"identifier": "main", "enabled": False}]},
        {"character_id": 100001, "order": [
            {"identifier": "chatHistory", "enabled": True},
            {"identifier": "style", "enabled": True},
            {"identifier": "main", "enabled": True},
            {"identifier": "off", "enabled": False},
        ]},
    ],
}


def test_parse_st_preset_follows_order_and_skips_markers():
    entries, skipped = parse_st_preset(ST_PRESET)
    assert [e.id for e in entries] == ["style", "main", "off", "orphan"]
    assert [e.enabled for e in entries] == [True, True, False, False]
    assert entries[2].role == "user"
    assert skipped == 3  # 两个占位 + 一个空条目


def test_parse_st_preset_without_order_keeps_file_order():
    entries, _ = parse_st_preset({"prompts": [{"identifier": "a", "content": "A"},
                                              {"identifier": "b", "content": "B", "enabled": False}]})
    assert [(e.id, e.enabled) for e in entries] == [("a", True), ("b", False)]


@pytest.mark.parametrize("bad", [[], {"prompts": "x"}, {"prompts": [{"identifier": "m", "marker": True}]}])
def test_parse_st_preset_rejects(bad):
    with pytest.raises(PresetError):
        parse_st_preset(bad)


def test_clean_entries_validates():
    entries = clean_entries([{"id": "a", "content": "x"}, {"id": "a", "content": "y"}, {"content": "z"}])
    assert len({e.id for e in entries}) == 3, "duplicate and missing ids are regenerated"
    for bad in ("x", [{"role": "tool"}], [{"content": 5}], [{"content": "x" * 20001}], [{}] * 301):
        with pytest.raises(PresetError):
            clean_entries(bad)


def test_expand_macros():
    rng = random.Random(0)
    text = expand_macros("{{char}}对{{user}}说{{newline}}嗯 {{trim}} 好{{unknown}}{{random:甲,乙}}{{// 备注}}",
                         char="凛音", user="千树", rng=rng)
    assert text.startswith("凛音对千树说\n嗯好") and text[-1] in "甲乙"
    assert expand_macros("{{random::a::b}}", char="", user="", rng=rng) in {"a", "b"}


def test_active_modes_scope():
    cfg = {"nsfw_enabled": True, "nsfw_users": ["111"], "nsfw_groups": ["900"],
           "aggressive_enabled": True, "aggressive_groups": ["800"]}
    assert active_modes(cfg, "111", None) == ["nsfw", "aggressive"]
    assert active_modes(cfg, "222", None) == ["aggressive"], "nsfw private needs whitelist"
    assert active_modes(cfg, "111", "700") == [], "whitelisted user in a non-whitelisted group"
    assert active_modes(cfg, "222", "900") == ["nsfw"]
    assert active_modes(cfg, "222", "800") == ["aggressive"]
    assert active_modes({**cfg, "nsfw_enabled": False, "aggressive_enabled": False}, "111", None) == []


def test_store_roundtrip_and_build_prompt(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    entries, _ = parse_st_preset(ST_PRESET)
    store.save("nsfw", "测试", entries)
    store.save("aggressive", "空", [])
    with pytest.raises(KeyError):
        store.save("nope", "", [])

    reloaded = PresetStore(tmp_path / "presets.json")
    assert [e.id for e in reloaded.entries("nsfw")] == ["style", "main", "off", "orphan"]
    assert reloaded.describe_mode("nsfw")["name"] == "测试"

    cfg = {"nsfw_enabled": True, "nsfw_users": ["111"], "aggressive_enabled": True}
    prompt, used = build_prompt(reloaded, cfg, "111", None, char="凛音", user="千树")
    assert used == ["nsfw"], "a mode with no enabled entries does not apply"
    assert prompt.startswith("【NSFW 模式】\n说话短一点。\n\n你是凛音，正在和千树聊天。")
    assert MODES["nsfw"].guard in prompt and "不应该出现" not in prompt

    reloaded.clear("nsfw")
    assert build_prompt(reloaded, cfg, "111", None, char="凛音", user="千树") == ("", [])


def test_store_ignores_broken_entries(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"nsfw": {"entries": [{"role": "bad"}]}, "unknown": {}, "aggressive": {
        "name": "ok", "entries": [{"id": "a", "content": "x"}]}}), encoding="utf-8")
    store = PresetStore(path)
    assert store.entries("nsfw") == [] and [e.id for e in store.entries("aggressive")] == ["a"]


def _local_example(name: str):
    """示例预设只放在本地（见 .gitignore），没有时跳过。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "mytest/plugins/_shared/presets/examples" / name
    if not path.exists():
        pytest.skip(f"本地没有 {name}")
    return parse_st_preset(json.loads(path.read_text(encoding="utf-8")))


def test_local_aggressive_example_imports():
    """本地的凛音强攻击力预设能直接导入，展开后不残留宏，并且保留停火条目。"""
    entries, skipped = _local_example("rinne_aggressive.json")
    assert skipped == 0 and len(entries) == 7 and all(e.enabled for e in entries)
    text = render(MODES["aggressive"], entries, char="白鸟凛音", user="千树")
    assert "{{" not in text and "白鸟凛音" in text and MODES["aggressive"].guard in text
    assert "～" not in text and "~" not in text
    assert "想伤害自己" in next(e for e in entries if e.id == "rinne_aggr_stop").content


def test_local_nsfw_example_imports():
    entries, skipped = _local_example("rinne_nsfw.json")
    assert skipped == 0 and len(entries) == 9 and all(e.enabled for e in entries)
    text = render(MODES["nsfw"], entries, char="白鸟凛音", user="千树")
    assert "{{" not in text and MODES["nsfw"].guard in text
    assert "马上停下" in next(e for e in entries if e.id == "rinne_nsfw_limits").content
    assert "工作时间" in next(e for e in entries if e.id == "rinne_nsfw_gate").content


# ---------------------------------------------------------------- WebUI

HEAD = {"X-Requested-With": "mytest-webui"}


def make_client(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    app = FastAPI()
    app.include_router(create_router(SettingsStore(tmp_path / "s.json", env_lookup=lambda n: None),
                                     lambda: "correct-horse-battery", guard=LoginGuard(), presets=store))
    client = TestClient(app)
    return client, store


def test_webui_preset_endpoints(tmp_path):
    client, store = make_client(tmp_path)
    body = json.dumps(ST_PRESET).encode()
    assert client.post("/webui/api/presets/nsfw/import", content=body, headers=HEAD).status_code == 401
    client.post("/webui/api/login", json={"password": "correct-horse-battery"}, headers=HEAD)

    assert client.post("/webui/api/presets/nsfw/import", content=body).status_code == 403, "needs header"
    assert client.post("/webui/api/presets/nope/import", content=body, headers=HEAD).status_code == 404
    r = client.post("/webui/api/presets/nsfw/import", content=b"\xef\xbb\xbf" + body,
                    headers={**HEAD, "X-Preset-Name": "%E6%B5%8B%E8%AF%95"})
    assert r.status_code == 200 and "4 条" in r.json()["message"] and "跳过了 3 条" in r.json()["message"]
    assert store.describe_mode("nsfw")["name"] == "测试"

    r = client.post("/webui/api/presets/nsfw/import", content=b"{not json", headers=HEAD)
    assert r.status_code == 422 and "JSON" in r.json()["message"]
    r = client.post("/webui/api/presets/nsfw/import", content=b"{}", headers=HEAD)
    assert r.status_code == 422 and "prompts" in r.json()["message"]
    big = b" " * (2 * 1024 * 1024 + 1)
    assert client.post("/webui/api/presets/nsfw/import", content=big, headers=HEAD).status_code == 413

    presets = client.get("/webui/api/settings").json()["presets"]
    assert [m["id"] for m in presets] == ["nsfw", "aggressive"] and len(presets[0]["entries"]) == 4

    entries = presets[0]["entries"][::-1]
    entries[0]["enabled"] = True
    r = client.put("/webui/api/presets/nsfw", json={"name": "改过", "entries": entries}, headers=HEAD)
    assert r.status_code == 200 and r.json()["preset"]["entries"][0]["id"] == "orphan"
    assert store.entries("nsfw")[0].enabled is True
    r = client.put("/webui/api/presets/nsfw", json={"entries": [{"role": "tool"}]}, headers=HEAD)
    assert r.status_code == 422
    assert client.put("/webui/api/presets/nsfw", json=[1], headers=HEAD).status_code == 400

    assert client.delete("/webui/api/presets/nsfw", headers=HEAD).json()["preset"]["entries"] == []
    assert store.entries("nsfw") == []


# ---------------------------------------------------------------- 工具接口


def test_preset_tool(monkeypatch, tmp_path):
    from mytest.plugins._shared import presets
    from mytest.plugins.tools_api_plugin.tools import TOOLS, ToolContext

    store = PresetStore(tmp_path / "p.json")
    store.save("aggressive", "", clean_entries([{"content": "毒舌一点，{{user}}。"}]))
    monkeypatch.setattr(presets, "preset_store", store)
    monkeypatch.setattr(presets, "preset_settings", {
        "char_name": "凛音", "aggressive_enabled": True, "aggressive_groups": [], "nsfw_enabled": False,
    })
    result = asyncio.run(TOOLS["preset"].run(ToolContext("123456", None, user_name="千树"), {}))
    assert result["modes"] == ["aggressive"] and "毒舌一点，千树。" in result["prompt"]
    result = asyncio.run(TOOLS["preset"].run(ToolContext("123456", "654321"), {}))
    assert result == {"content": "", "prompt": "", "modes": []}


# ---------------------------------------------------------------- MaiBot 插件


def test_rinne_tools_inject_preset(monkeypatch):
    from tests.test_tools import TOKEN, _load_rinne_tools, _tools_plugin

    mod = _load_rinne_tools()
    info = {"user_info": {"user_id": "1196477724", "user_cardname": "千树"}, "group_info": {"group_id": "123456"}}
    plugin, logs, _ = _tools_plugin(mod, {"555": {"message_id": "555", "message_info": info, "raw_message": []}})
    cfg = mod.RinneToolsConfig()
    cfg.plugin.token = TOKEN
    plugin._cfg = lambda: cfg
    requests = []
    reply = {"ok": True, "prompt": "【强攻击力模式】\n毒舌", "modes": ["aggressive"]}

    async def fake_request(tool, body, timeout):
        requests.append((tool, body))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(plugin, "_request", fake_request)
    kwargs = {"reply_message_id": "555", "session_id": "s1", "extra_prompt": "【好感度插件】x", "other": 1}

    assert asyncio.run(plugin.inject_preset(**kwargs)) == {"action": "continue"}, "off by default"
    cfg.preset.enabled = True
    result = asyncio.run(plugin.inject_preset(**kwargs))
    extra = result["modified_kwargs"]["extra_prompt"]
    assert extra.startswith("【好感度插件】x\n\n") and mod.PRESET_MARKER in extra and "毒舌" in extra
    assert result["modified_kwargs"]["other"] == 1
    assert requests[0][0] == "preset" and requests[0][1]["user_id"] == "1196477724"
    assert requests[0][1]["group_id"] == "123456" and requests[0][1]["args"] == {}

    assert "modified_kwargs" not in asyncio.run(plugin.inject_preset(**{**kwargs, "extra_prompt": extra})), \
        "retry does not inject twice"
    assert "modified_kwargs" not in asyncio.run(plugin.inject_preset(**{**kwargs, "reply_message_id": "999"}))

    reply = {"ok": True, "prompt": "", "modes": []}
    assert "modified_kwargs" not in asyncio.run(plugin.inject_preset(**kwargs))
    import httpx

    reply = httpx.ConnectError("down")
    assert "modified_kwargs" not in asyncio.run(plugin.inject_preset(**kwargs))
    assert any("取聊天预设失败" in line for line in logs)
