"""一起听 M4：凛音的推荐、发歌、一起听状态，以及 MaiBot 插件的注入钩子。

运行：python -m pytest tests/test_listen_rinne.py -q
（MaiBot 那部分需要 maibot-plugin-sdk，没装时跳过）
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from mytest.plugins.listen_plugin.card import CARD_TYPE, build_card, jump_url
from mytest.plugins.listen_plugin.recommend import (
    RecommendRequest,
    build_prompt,
    parse_songs,
    pick_match,
    recommend,
)
from mytest.plugins.listen_plugin.service import ListenConfig, ListenService
from mytest.plugins.listen_plugin.sources.base import SourceError, Stream, Track
from mytest.plugins.listen_plugin.sources.resolver import Sources
from mytest.plugins.listen_plugin.store import Store
from mytest.plugins.tools_api_plugin import tools as tools_mod
from mytest.plugins.tools_api_plugin.tools import TOOLS, ToolContext, ToolError

from .test_listen import GROUP, ORIGIN, SECRET, FakeBridge
from .test_tools import _load_rinne_tools


def run(coro: Any) -> Any:
    return asyncio.run(coro)


QINGTIAN = Track("qq", "0039MnYb0qxYhV", "晴天", "周杰伦", duration_ms=269_000, vip=True)
QINGTIAN_LIVE = Track("qq", "0039MnYb0qxYh1", "晴天 (Live)", "周杰伦", duration_ms=299_000)
LEMON = Track("netease", "536622304", "Lemon", "米津玄師", duration_ms=255_000)
FAKE_SONG = ("这首歌不存在", "某歌手")


class Catalog:
    """按关键词返回固定结果的假平台。"""

    def __init__(self, platform: str, tracks: list[Track], vip_ready: bool = False) -> None:
        self.platform = platform
        self.tracks = tracks
        self.vip_ready = vip_ready
        self.queries: list[str] = []

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        self.queries.append(keyword)
        return [t for t in self.tracks if t.title.split(" ")[0] in keyword][:limit]

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        if track.vip and not self.vip_ready:
            raise SourceError("会员歌")
        return Stream(f"https://cdn.example/{track.platform}/{track.id}.mp3", 3600, bitrate)

    async def lyrics(self, track: Track) -> tuple[str, str]:
        return "", ""


# ---------------------------------------------------------------- 推荐：解析和核对


def test_parse_songs():
    assert parse_songs('{"songs": [{"title": "晴天", "artist": "周杰伦"}]}') == [("晴天", "周杰伦")]
    assert parse_songs('<think>嗯</think>好的：{"songs":[{"title":"Lemon","artist":"米津玄師"},{"title":""}]}') == [
        ("Lemon", "米津玄師")]
    for bad in ("", "晴天 - 周杰伦", '{"songs": "晴天"}', '{"songs": []}', "{bad json"):
        assert parse_songs(bad) is None, bad


def test_pick_match_rejects_wrong_versions_and_artists():
    results = [QINGTIAN_LIVE, Track("qq", "x" * 14, "晴天", "Lucky小爱"), QINGTIAN]
    assert pick_match("晴天", "周杰伦", results) == QINGTIAN, "skips the Live version and the cover singer"
    assert pick_match("晴天 (Live)", "周杰伦", results) == QINGTIAN_LIVE, "asked for Live explicitly"
    assert pick_match("晴天", "林俊杰", results) is None
    assert pick_match("Lemon", "米津玄師", [Track("netease", "1", "Lemon (Original)", "米津玄師")]) is not None


def test_prompt_puts_request_first_and_lists_played():
    req = RecommendRequest(want="安静一点适合睡前的", chat=["A：好困"], played=["晴天 - 周杰伦"], taste=["Lemon - 米津玄師"])
    prompt = build_prompt(req, 2)
    assert prompt.startswith("要求：安静一点适合睡前的")
    assert "不要选这些" in prompt and "晴天 - 周杰伦" in prompt
    assert "仅供参考" in prompt
    empty = build_prompt(RecommendRequest(want=""), 1)
    assert "随便" in empty and "群里" not in empty


def fake_llm(replies: list[str]):
    calls: list[list[dict]] = []

    async def chat(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return replies.pop(0) if replies else '{"songs": []}'

    return chat, calls


def test_recommend_drops_made_up_songs_and_retries():
    sources = Sources({"qq": Catalog("qq", [QINGTIAN]), "netease": Catalog("netease", [LEMON])})
    chat, calls = fake_llm([
        json.dumps({"songs": [{"title": FAKE_SONG[0], "artist": FAKE_SONG[1]}]}),
        json.dumps({"songs": [{"title": "Lemon", "artist": "米津玄師"}]}),
    ])
    found = run(recommend(RecommendRequest(want="日语"), sources, playable=lambda t: not t.vip, chat=chat))
    assert found == [LEMON]
    assert len(calls) == 2
    assert FAKE_SONG[0] in calls[1][1]["content"], "tells the model which ones were not found"


def test_recommend_prefers_playable_copy():
    free_qingtian = Track("netease", "186016", "晴天", "周杰伦", duration_ms=269_000)
    sources = Sources({"qq": Catalog("qq", [QINGTIAN]), "netease": Catalog("netease", [free_qingtian])})
    chat, _ = fake_llm([json.dumps({"songs": [{"title": "晴天", "artist": "周杰伦"}]})])
    found = run(recommend(RecommendRequest(want="周杰伦"), sources, playable=lambda t: not t.vip, chat=chat))
    assert found == [free_qingtian], "QQ copy is VIP, NetEase copy is free"


def test_recommend_skips_recently_played():
    sources = Sources({"netease": Catalog("netease", [LEMON])})
    chat, calls = fake_llm([json.dumps({"songs": [{"title": "Lemon", "artist": "米津玄師"}]})] * 3)
    found = run(recommend(RecommendRequest(want="x", played=["Lemon - 米津玄師"]), sources,
                          playable=lambda t: True, chat=chat))
    assert found == [] and len(calls) == 3


def test_recommend_bad_json_three_times_raises():
    from mytest.plugins._shared.services.http import ServiceError

    sources = Sources({"netease": Catalog("netease", [LEMON])})
    chat, calls = fake_llm(["不好意思", "还是不行", "抱歉"])
    with pytest.raises(ServiceError, match="格式"):
        run(recommend(RecommendRequest(want="x"), sources, playable=lambda t: True, chat=chat))
    assert len(calls) == 3


def test_recommend_count_is_capped():
    tracks = [Track("netease", str(i), f"歌{i}", "甲", duration_ms=1) for i in range(10)]
    sources = Sources({"netease": Catalog("netease", tracks)})
    chat, _ = fake_llm([json.dumps({"songs": [{"title": f"歌{i}", "artist": "甲"} for i in range(10)]})])
    found = run(recommend(RecommendRequest(want="x", count=99), sources, playable=lambda t: True, chat=chat))
    assert len(found) == 5


# ---------------------------------------------------------------- 卡片


def test_card_fields():
    card = build_card(LEMON, "https://music.163.com/song/media/outer/url?id=536622304.mp3")
    data = card.segment_data()
    assert data["type"] == "163" and data["url"] == jump_url(LEMON) and data["title"] == "Lemon"
    assert data["content"] == "米津玄師" and data["image"].startswith("https://")
    assert set(CARD_TYPE.values()) == {"qq", "163", "kugou"}
    no_audio = build_card(QINGTIAN, "").segment_data()
    assert "audio" not in no_audio, "empty audio is dropped, the card still jumps to the song page"
    assert "y.qq.com" in build_card(QINGTIAN, "").fallback_text()


# ---------------------------------------------------------------- NoneBot 工具


@pytest.fixture
def listen(monkeypatch):
    """装一个假的一起听服务给 tools_api_plugin 用，并拦下发到 QQ 的消息。"""
    bridge = FakeBridge()
    bridge.add("1", "房主")
    free_qingtian = Track("netease", "186016", "晴天", "周杰伦", duration_ms=269_000)
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=bridge, store=Store(":memory:"),
        source_map={"qq": Catalog("qq", [QINGTIAN, QINGTIAN_LIVE]), "netease": Catalog("netease", [LEMON, free_qingtian])},
    )
    monkeypatch.setattr(tools_mod, "_listen", lambda: service)
    sent: list[Any] = []

    async def fake_send(ctx, message, *, quote=False):
        sent.append(message)

    monkeypatch.setattr(tools_mod, "send", fake_send)
    return service, sent


def ctx(group: str | None = GROUP) -> ToolContext:
    return ToolContext(user_id="1", group_id=group, user_name="房主")


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_listen_context_only_when_playing(listen):
    service, _ = listen
    assert run(TOOLS["listen_context"].run(ctx(), {}))["content"] == ""
    room, _ = service.hub.open(GROUP, "1")
    assert run(TOOLS["listen_context"].run(ctx(), {}))["content"] == "", "room open but nothing playing"
    room.add(LEMON, "1", "房主")
    room.add(QINGTIAN_LIVE, "1", "房主")
    room.play("1", service.clock())
    text = run(TOOLS["listen_context"].run(ctx(), {}))["content"]
    assert text.startswith("本群正在一起听：Lemon - 米津玄師（0:00/4:15，房主点的）")
    assert "接下来：晴天 (Live) - 周杰伦" in text
    assert run(TOOLS["listen_context"].run(ctx("999999"), {}))["content"] == "", "not a whitelisted group"


def test_listen_status(listen):
    service, _ = listen
    assert "没有一起听" in run(TOOLS["listen_status"].run(ctx(), {}))["content"]
    room, _ = service.hub.open(GROUP, "1")
    room.add(LEMON, "1", "房主")
    room.play("1", service.clock())
    room.add(QINGTIAN_LIVE, "1", "房主")
    out = run(TOOLS["listen_status"].run(ctx(), {}))["content"]
    assert "正在放：Lemon" in out and "接下来：晴天 (Live)" in out
    with pytest.raises(ToolError):
        run(TOOLS["listen_status"].run(ctx("999999"), {}))


def test_music_share_sends_card(listen):
    _, sent = listen

    async def go():
        result = await TOOLS["music_share"].run(ctx(), {"song": "Lemon 米津玄師"})
        await settle()
        return result

    result = run(go())
    assert "Lemon - 米津玄師" in result["content"]
    seg = sent[-1][0]
    # NapCat（msg.ts）：不带 id 时按自定义卡片处理，type 是平台（163 显示「网易云音乐」角标），url 和 image 必填
    assert seg.type == "music" and seg.data["type"] == "163" and "id" not in seg.data
    assert seg.data["url"] and seg.data["image"]
    assert seg.data["audio"] == "https://music.163.com/song/media/outer/url?id=536622304.mp3", \
        "NetEase card uses the long-lived official link"
    with pytest.raises(ToolError, match="没搜到"):
        run(TOOLS["music_share"].run(ctx(None), {"song": "不存在的歌"}))


def test_music_share_prefers_original_and_playable(listen):
    """QQ 上有会员原版和免费 Live 版，网易云有免费原版：发网易云的原版。"""
    _, sent = listen

    async def go():
        result = await TOOLS["music_share"].run(ctx(), {"song": "晴天"})
        await settle()
        return result

    result = run(go())
    assert "「晴天 - 周杰伦」" in result["content"]
    assert sent[-1][0].data["type"] == "163", "QQ copy is VIP without an account, NetEase copy is free"

    async def live():
        return await TOOLS["music_share"].run(ctx(), {"song": "晴天 Live"})

    assert "(Live)" in run(live())["content"], "asking for Live gets the Live version"


def test_music_share_never_swaps_in_a_cover(listen):
    """原曲只有会员版、别的平台只有翻唱的同名歌：发原曲（图文卡片，点开跳原曲），不发翻唱。"""
    service, sent = listen
    cover = Track("netease", "2652820720", "晴天", "Lucky小爱", duration_ms=278_000)
    service.source_map["netease"].tracks = [cover]  # type: ignore[attr-defined]

    async def go():
        result = await TOOLS["music_share"].run(ctx(), {"song": "晴天"})
        await settle()
        return result

    assert "「晴天 - 周杰伦」" in run(go())["content"]
    data = sent[-1][0].data
    assert data["type"] == "qq" and "audio" not in data, "VIP without an account: no audio, jumps to QQ Music"


def test_card_falls_back_to_text(listen, monkeypatch):
    _, sent = listen
    calls = []

    async def flaky_send(ctx, message, *, quote=False):
        calls.append(message)
        if message[0].type == "music":
            raise RuntimeError("签名服务挂了")
        sent.append(message)

    monkeypatch.setattr(tools_mod, "send", flaky_send)

    async def go():
        await TOOLS["music_share"].run(ctx(), {"song": "Lemon"})
        await settle()

    run(go())
    assert [m[0].type for m in calls] == ["music", "image"]
    assert "music.163.com" in str(sent[-1])


def fake_recommender(monkeypatch, songs: list[tuple[str, str]]):
    from mytest.plugins.listen_plugin import recommend as rec_mod

    async def chat(messages):
        return json.dumps({"songs": [{"title": t, "artist": a} for t, a in songs]})

    original = rec_mod.recommend

    async def patched(req, sources, *, playable, chat_=None, **kw):
        patched.req = req  # type: ignore[attr-defined]
        return await original(req, sources, playable=playable, chat=chat)

    monkeypatch.setattr(rec_mod, "recommend", patched)
    return patched


def test_recommend_adds_to_room_when_listening(listen, monkeypatch):
    service, sent = listen
    patched = fake_recommender(monkeypatch, [("Lemon", "米津玄師")])
    room, _ = service.hub.open(GROUP, "1")
    room.add(QINGTIAN_LIVE, "1", "房主")
    room.play("1", service.clock())
    room.next("1", service.clock() + 1)  # 晴天 (Live) 进历史

    async def go():
        return await TOOLS["music_recommend"].run(ctx(), {"want": "日语的", "recent_chat": ["A：困了", 3]})

    result = run(go())
    assert "加进本群一起听的队列" in result["content"]
    assert room.current is not None and room.current.track == LEMON, "empty room starts playing it"
    assert room.current.added_by_name == "凛音"
    assert not sent, "no card when it went into the room"
    assert patched.req.want == "日语的" and patched.req.chat == ["A：困了"], "non-string chat lines are dropped"
    assert "晴天 (Live) - 周杰伦" in patched.req.played


def test_recommend_sends_card_without_room(listen, monkeypatch):
    _, sent = listen
    fake_recommender(monkeypatch, [("Lemon", "米津玄師")])

    async def go():
        result = await TOOLS["music_recommend"].run(ctx(None), {"want": "随便"})
        await settle()
        return result

    result = run(go())
    assert "卡片马上发出去" in result["content"]
    assert sent and sent[-1][0].type == "music"


def test_recommend_vip_only_in_room_is_rejected(listen, monkeypatch):
    service, _ = listen
    fake_recommender(monkeypatch, [("晴天", "周杰伦")])
    service.source_map["netease"].tracks = [LEMON]  # type: ignore[attr-defined]  去掉网易云的免费晴天
    service.hub.open(GROUP, "1")
    with pytest.raises(ToolError, match="会员"):
        run(TOOLS["music_recommend"].run(ctx(), {"want": "周杰伦"}))


def test_tools_without_listen(monkeypatch):
    monkeypatch.setattr(tools_mod, "_listen", lambda: None)
    assert run(TOOLS["listen_context"].run(ctx(), {}))["content"] == ""
    for name, args in (("music_share", {"song": "x"}), ("music_recommend", {"want": "x"}), ("listen_status", {})):
        with pytest.raises(ToolError, match="没有开启"):
            run(TOOLS[name].run(ctx(), args))


# ---------------------------------------------------------------- MaiBot 插件


def maibot_plugin(responses: dict[str, Any], messages: dict[str, Any] | None = None, recent: Any = None):
    mod = _load_rinne_tools()
    plugin = mod.RinneToolsPlugin()
    logs: list[str] = []
    requests: list[tuple[str, dict, float]] = []

    async def get_by_id(message_id, *, stream_id=""):
        return {"success": True, "message": (messages or {}).get(message_id)}

    async def get_recent(chat_id, limit=20):
        return recent or []

    logger = SimpleNamespace(info=lambda *a: logs.append(a[0] % a[1:]), warning=lambda *a: logs.append(a[0] % a[1:]),
                             debug=lambda *a: None)
    plugin._set_context(SimpleNamespace(message=SimpleNamespace(get_by_id=get_by_id, get_recent=get_recent),
                                        logger=logger))
    cfg = mod.RinneToolsConfig()
    cfg.plugin.token = "t" * 32
    plugin._cfg = lambda: cfg

    async def request(tool, body, timeout):
        requests.append((tool, body, timeout))
        reply = responses.get(tool)
        if isinstance(reply, Exception):
            raise reply
        return reply or {"ok": True, "content": ""}

    plugin._request = request
    return mod, plugin, cfg, requests, logs


GROUP_MSG = {"message_id": "555", "message_info": {"user_info": {"user_id": "42", "user_cardname": "千树"},
                                                   "group_info": {"group_id": GROUP}}, "raw_message": []}


def test_maibot_injects_listening_context():
    pytest.importorskip("maibot_sdk")
    mod, plugin, cfg, requests, _ = maibot_plugin(
        {"listen_context": {"ok": True, "content": "本群正在一起听：Lemon - 米津玄師（1:00/4:15）。"}},
        {"555": GROUP_MSG},
    )
    kwargs = {"session_id": "s1", "reply_message_id": "555", "extra_prompt": "【好感度插件】……"}
    out = run(plugin.inject_listening(**kwargs))
    extra = out["modified_kwargs"]["extra_prompt"]
    assert extra.startswith("【好感度插件】……") and "【一起听】本群正在一起听：Lemon" in extra
    assert "不要主动说起" in extra
    assert requests[0][0] == "listen_context" and requests[0][1]["group_id"] == GROUP and requests[0][2] == 1.0
    assert kwargs["extra_prompt"] == "【好感度插件】……", "caller's kwargs are not modified in place"
    again = run(plugin.inject_listening(**out["modified_kwargs"]))
    assert "modified_kwargs" not in again, "already injected"
    run(plugin.inject_listening(session_id="s1", reply_message_id="555", extra_prompt=""))
    assert len(requests) == 1, "cached for 10 seconds, session → group also cached"

    cfg.tools.listen_context = False
    assert "modified_kwargs" not in run(plugin.inject_listening(session_id="s1", reply_message_id="555"))


def test_maibot_injection_never_breaks_replies():
    pytest.importorskip("maibot_sdk")
    import httpx

    _, plugin, _, _, _ = maibot_plugin({"listen_context": httpx.ConnectTimeout("slow")}, {"555": GROUP_MSG})
    assert run(plugin.inject_listening(session_id="s1", reply_message_id="555")) == {"action": "continue"}
    private = {"555": {**GROUP_MSG, "message_info": {"user_info": {"user_id": "42"}}}}
    _, plugin2, _, requests, _ = maibot_plugin({}, private)
    assert run(plugin2.inject_listening(session_id="p1", reply_message_id="555")) == {"action": "continue"}
    assert not requests, "private chats never ask NoneBot"
    _, plugin3, _, _, _ = maibot_plugin({"listen_context": {"ok": True, "content": ""}}, {"555": GROUP_MSG})
    assert "modified_kwargs" not in run(plugin3.inject_listening(session_id="s1", reply_message_id="555"))


def test_maibot_recommend_sends_recent_chat():
    pytest.importorskip("maibot_sdk")
    recent = [
        {"message_info": {"user_info": {"user_cardname": "甲"}}, "processed_plain_text": "今天好累"},
        {"message_info": {"user_info": {"user_nickname": "乙"}}, "processed_plain_text": ""},
        {"message": {"message_info": {"user_info": {"user_nickname": "丙"}}, "processed_plain_text": "想听点安静的"}},
    ]
    _, plugin, _, requests, _ = maibot_plugin({"music_recommend": {"ok": True, "content": "已经加进队列"}},
                                              {"555": GROUP_MSG}, recent)
    result = run(plugin.rinne_music_recommend(want="安静的", msg_id="555", stream_id="s1", group_id=GROUP))
    assert result == {"success": True, "content": "已经加进队列"}
    tool, body, _ = requests[0]
    assert tool == "music_recommend" and body["user_id"] == "42"
    assert body["args"] == {"want": "安静的", "count": 1, "recent_chat": ["甲：今天好累", "丙：想听点安静的"]}


def test_maibot_music_switch():
    pytest.importorskip("maibot_sdk")
    _, plugin, cfg, requests, _ = maibot_plugin({}, {"555": GROUP_MSG})
    cfg.tools.music = False
    result = run(plugin.rinne_music_share(song="晴天", msg_id="555", stream_id="s1"))
    assert result["success"] is False and not requests
    assert "modified_kwargs" not in run(plugin.inject_listening(session_id="s1", reply_message_id="555"))


def test_maibot_hook_is_declared():
    pytest.importorskip("maibot_sdk")
    mod = _load_rinne_tools()
    from maibot_sdk.components import collect_components

    comps = collect_components(mod.RinneToolsPlugin())
    text = json.dumps(comps, ensure_ascii=False, default=str)
    for name in ("rinne_music_share", "rinne_music_recommend", "rinne_listen_status", "inject_listening",
                 "maisaka.replyer.before_request"):
        assert name in text, name
