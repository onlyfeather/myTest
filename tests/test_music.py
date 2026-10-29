"""点歌：三个平台一起搜、编号列表、回复编号发卡片、bot 会员账号。

运行：python -m pytest tests/test_music.py -q
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import pytest

from mytest.plugins.music_plugin.accounts.base import AccountInfo, CredentialExpired, QrStart, QrState
from mytest.plugins.music_plugin.crypto import Vault
from mytest.plugins.music_plugin.picker import Picker, format_list, number_choices
from mytest.plugins.music_plugin.service import MusicService
from mytest.plugins.music_plugin.sources.base import SourceError, SourceUnavailable, Stream, Track, VipCredential
from mytest.plugins.music_plugin.store import Store
from mytest.plugins.music_plugin.vip import BOT, REFRESH_INTERVAL_MS, VipAccounts, VipError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


QQ_SONG = Track("qq", "0039MnYb0qxYhV", "晴天", "周杰伦", duration_ms=269_000, vip=True)
QQ_LIVE = Track("qq", "0039MnYb0qxYh1", "晴天 (Live)", "周杰伦", duration_ms=299_000)
NCM_SONG = Track("netease", "186016", "晴天", "周杰伦", duration_ms=269_500)
KG_SONG = Track("kugou", "B" * 32, "晴天", "周杰伦", duration_ms=269_000, vip=True)


class FakeSource(VipCredential):
    def __init__(self, platform: str, tracks: list[Track], *, error: Exception | None = None) -> None:
        super().__init__()
        self.platform = platform
        self.tracks = tracks
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        self.calls.append((keyword, limit))
        if self.error:
            raise self.error
        return self.tracks[:limit]

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        if track.vip and not self.vip_ready:
            raise SourceError("会员歌")
        return Stream(f"https://cdn.example/{track.platform}/{track.id}.mp3", 3600, bitrate)

    async def lyrics(self, track: Track) -> tuple[str, str]:
        return "", ""


def three(**errors: Exception) -> tuple[MusicService, dict[str, FakeSource]]:
    sources = {
        "kugou": FakeSource("kugou", [KG_SONG], error=errors.get("kugou")),
        "qq": FakeSource("qq", [QQ_SONG, QQ_LIVE], error=errors.get("qq")),
        "netease": FakeSource("netease", [NCM_SONG], error=errors.get("netease")),
    }
    return MusicService(dict(sources)), sources


# ---------------------------------------------------------------- 三个平台一起搜


def test_search_all_three_platforms_in_fixed_order():
    service, sources = three()
    results = run(service.search_all("晴天", 3))
    assert [r.platform for r in results] == ["qq", "netease", "kugou"], "fixed order regardless of dict order"
    assert [len(r.tracks) for r in results] == [2, 1, 1]
    assert all(s.calls == [("晴天", 3)] for s in sources.values()), "every platform is searched once"
    assert service.known_track(KG_SONG.ref) == KG_SONG, "results are remembered"


def test_search_all_one_platform_failing_doesnt_hide_others():
    service, _ = three(kugou=SourceUnavailable("酷狗接口连不上"), qq=SourceError("搜索出错"))
    results = {r.platform: r for r in run(service.search_all("晴天", 3))}
    assert results["netease"].tracks == [NCM_SONG]
    assert results["kugou"].error == "酷狗接口连不上" and not results["kugou"].tracks
    assert results["qq"].error == "搜索出错"


def test_search_all_runs_platforms_concurrently():
    class Slow(FakeSource):
        async def search(self, keyword: str, limit: int = 20) -> list[Track]:
            await asyncio.sleep(0.2)
            return await super().search(keyword, limit)

    service = MusicService({p: Slow(p, []) for p in ("qq", "netease", "kugou")})

    async def go() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await service.search_all("x")
        return loop.time() - start

    assert run(go()) < 0.5, "three 0.2s searches finish together"


def test_missing_platform_is_simply_absent():
    service = MusicService({"qq": FakeSource("qq", [QQ_SONG])})
    assert [r.platform for r in run(service.search_all("晴天"))] == ["qq"]


def test_resolve_song_picks_playable_original():
    service, _ = three()
    assert run(service.resolve_song("晴天")) == NCM_SONG, "QQ original is VIP; NetEase copy is free"
    assert run(service.resolve_song("晴天 Live")) == QQ_LIVE


def test_card_audio():
    service, sources = three()
    assert run(service.card_audio(NCM_SONG)) == "https://music.163.com/song/media/outer/url?id=186016.mp3"
    assert run(service.card_audio(QQ_SONG)) == "", "VIP without account: no audio, card still sent"
    sources["qq"].bind_credential(lambda: {"cookie": "x"}, lambda m: None)
    assert run(service.card_audio(QQ_SONG)) == f"https://cdn.example/qq/{QQ_SONG.id}.mp3"


# ---------------------------------------------------------------- 编号列表和回复


def grouped() -> list[tuple[str, list[Track]]]:
    return [("qq", [QQ_SONG, QQ_LIVE]), ("netease", [NCM_SONG]), ("kugou", [KG_SONG])]


def test_numbering_is_continuous_across_platforms():
    choices = number_choices(grouped())
    assert [(c.number, c.track) for c in choices] == [(1, QQ_SONG), (2, QQ_LIVE), (3, NCM_SONG), (4, KG_SONG)]


def test_list_text_marks_vip_and_errors():
    groups = [("qq", [QQ_SONG, QQ_LIVE]), ("netease", []), ("kugou", [])]
    choices = number_choices(groups)
    text = format_list("晴天", groups, {"kugou": "酷狗接口连不上"}, choices, vip_ready=lambda p: False)
    assert "【QQ音乐】" in text and "1. 晴天 - 周杰伦 4:29 [会员·只能试听]" in text
    assert "2. 晴天 (Live) - 周杰伦 4:59" in text and "[会员" not in text.split("2. ")[1].split("\n")[0]
    assert "【网易云音乐】没搜到" in text and "【酷狗音乐】酷狗接口连不上" in text
    assert "回复编号" in text and "回复 0 取消" in text
    ready = format_list("晴天", groups, {}, choices, vip_ready=lambda p: True)
    assert "1. 晴天 - 周杰伦 4:29 [会员]" in ready


def test_picker_flow():
    clock = [0.0]
    picker = Picker(clock=lambda: clock[0])
    picker.offer("group:1", "42", number_choices(grouped()))
    assert picker.waiting("group:1", "42")
    assert not picker.waiting("group:1", "43"), "only the person who searched"
    assert not picker.waiting("group:2", "42"), "only in that chat"
    assert picker.take("group:1", "42", "你好") is None, "normal chat passes through"
    assert "没有 9 号" in picker.take("group:1", "42", "9"), "out of range keeps the list"
    assert picker.take("group:1", "42", " 3 ") == NCM_SONG
    assert picker.take("group:1", "42", "3") is None, "list is used up after a pick"


def test_picker_cancel_and_expiry():
    clock = [0.0]
    picker = Picker(clock=lambda: clock[0])
    picker.offer("group:1", "42", number_choices(grouped()))
    assert picker.take("group:1", "42", "0") == "好，不点了。"
    assert not picker.waiting("group:1", "42")
    picker.offer("group:1", "42", number_choices(grouped()))
    clock[0] = 121
    assert not picker.waiting("group:1", "42")
    assert picker.take("group:1", "42", "1") is None, "expired after 2 minutes"


def test_new_search_replaces_old_list():
    picker = Picker()
    picker.offer("group:1", "42", number_choices(grouped()))
    picker.offer("group:1", "42", number_choices([("netease", [NCM_SONG])]))
    assert picker.take("group:1", "42", "1") == NCM_SONG


# ---------------------------------------------------------------- bot 会员账号


class FakeAccounts:
    platform = "qq"
    login_kinds = ("qq", "wx")

    def __init__(self) -> None:
        self.state = QrState("waiting")
        self.refresh_error: Exception | None = None

    async def qr_start(self, kind: str) -> QrStart:
        return QrStart({"kind": kind, "secret": "s3cret"}, "data:image/png;base64,AAAA", "用 QQ 扫码")

    async def qr_poll(self, session: dict) -> QrState:
        return self.state

    async def account(self, credential: dict) -> AccountInfo:
        return AccountInfo("10001", "小号", vip=True)

    async def refresh(self, credential: dict) -> dict:
        if self.refresh_error:
            raise self.refresh_error
        return {**credential, "n": credential.get("n", 0) + 1}


def vault() -> Vault:
    return Vault(base64.b64decode(base64.b64encode(os.urandom(32))))


@pytest.fixture
def vip():
    clock = [1_000_000]
    store = Store(":memory:")
    acc = FakeAccounts()
    accounts = VipAccounts(store, {"qq": acc}, vault(), clock=lambda: clock[0])
    return accounts, acc, store, clock


def test_vip_qr_login_saves_only_ciphertext(vip):
    accounts, acc, store, _ = vip
    sid, start = run(accounts.qr_start("qq", "wx"))
    assert start.image.startswith("data:image")
    assert run(accounts.qr_poll(sid)) == {"status": "waiting", "message": ""}
    acc.state = QrState("done", {"cookie": "qqmusic_key=abc"})
    assert run(accounts.qr_poll(sid)) == {"status": "done", "account": "小号", "vip": True}
    row = store.credential(BOT, "qq")
    assert row is not None and b"qqmusic_key" not in row.ciphertext
    assert accounts.credential("qq") == {"cookie": "qqmusic_key=abc"}
    status = accounts.status()[0]
    assert status["logged_in"] and status["vip"] and status["account_name"] == "小号"
    assert status["login_kinds"] == ["qq", "wx"]
    assert "cookie" not in str(accounts.status()), "status never leaks the credential"
    assert run(accounts.qr_poll(sid))["status"] == "expired", "a finished QR session is gone"


def test_vip_unlocks_vip_songs_and_clears_cache(vip):
    accounts, acc, _, _ = vip
    source = FakeSource("qq", [QQ_SONG])
    service = MusicService({"qq": source}, accounts)
    assert not service.vip_ready("qq") and not service.playable(QQ_SONG)
    sid, _ = run(accounts.qr_start("qq", "qq"))
    acc.state = QrState("done", {"cookie": "x"})
    run(accounts.qr_poll(sid))
    assert service.vip_ready("qq") and service.playable(QQ_SONG)
    assert run(service.card_audio(QQ_SONG)).endswith(".mp3")
    source.credential_failed("登录失效了")
    assert not service.vip_ready("qq"), "a failed stream marks the account expired"
    assert accounts.status()[0]["expired"] and accounts.status()[0]["message"] == "登录失效了"
    accounts.logout("qq")
    assert not accounts.status()[0]["logged_in"] and not accounts.status()[0]["expired"]


def test_vip_needs_key():
    accounts = VipAccounts(Store(":memory:"), {"qq": FakeAccounts()}, None)
    with pytest.raises(VipError, match="MUSIC_CREDENTIAL_KEY"):
        run(accounts.qr_start("qq", "qq"))
    assert accounts.status()[0]["login_kinds"] == []


def test_vip_refresh_and_expiry(vip):
    accounts, acc, _, clock = vip
    sid, _ = run(accounts.qr_start("qq", "qq"))
    acc.state = QrState("done", {"cookie": "x"})
    run(accounts.qr_poll(sid))
    assert run(accounts.refresh_due()) == {"refreshed": 0, "expired": 0}, "not due yet"
    clock[0] += REFRESH_INTERVAL_MS

    async def no_sleep(_: float) -> None:
        return None

    import mytest.plugins.music_plugin.vip as vip_mod

    original = vip_mod.asyncio.sleep
    vip_mod.asyncio.sleep = no_sleep  # type: ignore[assignment]
    try:
        assert run(accounts.refresh_due()) == {"refreshed": 1, "expired": 0}
        assert accounts.credential("qq") == {"cookie": "x", "n": 1}
        clock[0] += REFRESH_INTERVAL_MS
        acc.refresh_error = CredentialExpired("登录过期了")
        assert run(accounts.refresh_due()) == {"refreshed": 0, "expired": 1}
    finally:
        vip_mod.asyncio.sleep = original  # type: ignore[assignment]
    assert accounts.credential("qq") is None and accounts.status()[0]["expired"]


def test_old_listen_database_still_works(tmp_path):
    """一起听时期的 listen.db 有更多表；只读 credentials，会员账号不用重新扫码。"""
    import sqlite3

    path = tmp_path / "listen.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, qq TEXT, group_id TEXT, expires_at INTEGER);
        CREATE TABLE favorites (qq TEXT, ref TEXT, track TEXT, added_at INTEGER);
    """)
    db.commit()
    db.close()
    key = vault()
    first = VipAccounts(Store(path), {"qq": FakeAccounts()}, key)
    acc = first.accounts["qq"]
    sid, _ = run(first.qr_start("qq", "qq"))
    acc.state = QrState("done", {"cookie": "x"})  # type: ignore[attr-defined]
    run(first.qr_poll(sid))
    first.store.close()
    again = VipAccounts(Store(path), {"qq": FakeAccounts()}, key)
    assert again.credential("qq") == {"cookie": "x"}


# ---------------------------------------------------------------- /点歌 指令


def test_credential_key_accepts_old_name(monkeypatch):
    from mytest.plugins.music_plugin import config

    monkeypatch.delenv("MUSIC_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("LISTEN_CREDENTIAL_KEY", "old-name")
    monkeypatch.setattr(config, "_env", lambda name: os.environ.get(name.upper(), ""))
    assert config.credential_keys()[0] == "old-name"
    monkeypatch.setenv("MUSIC_CREDENTIAL_KEY", "new-name")
    assert config.credential_keys()[0] == "new-name"


class FakeBot:
    def __init__(self, *, card_fails: bool = False) -> None:
        self.sent: list[Any] = []
        self.card_fails = card_fails

    async def send(self, event: Any, message: Any) -> None:
        if self.card_fails and message[0].type == "music":
            raise RuntimeError("签名服务挂了")
        self.sent.append(message)


class Finished(Exception):
    def __init__(self, message: Any = None) -> None:
        super().__init__(message)
        self.message = message


class FakeMatcher:
    async def finish(self, message: Any = None) -> None:
        raise Finished(message)


def group_event(text: str, user: str = "42", group: str = "100001") -> Any:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender

    return GroupMessageEvent(
        time=0, self_id=1, post_type="message", sub_type="normal", user_id=int(user), message_type="group",
        message_id=1, message=Message(text), original_message=Message(text), raw_message=text, font=0,
        sender=Sender(user_id=int(user)), group_id=int(group),
    )


@pytest.fixture
def plugin(monkeypatch):
    import mytest.plugins.music_plugin as mod

    service, _ = three()
    monkeypatch.setattr(mod, "music", service)
    monkeypatch.setattr(mod, "picks", Picker())

    async def not_superuser(bot, event):
        return False

    monkeypatch.setattr(mod, "is_superuser", not_superuser)
    from mytest.plugins._shared.limits import UsageLimiter

    monkeypatch.setattr(mod, "limiter", UsageLimiter())
    return mod


def say(handler: Any, *args: Any) -> Any:
    try:
        run(handler(*args))
    except Finished as e:
        return e.message
    return None


def test_song_command_lists_three_platforms_then_sends_card(plugin):
    bot = FakeBot()
    event = group_event("/点歌 晴天")
    text = say(plugin.handle_song, bot, event, FakeMatcher(), ("晴天",))
    assert "【QQ音乐】" in text and "【网易云音乐】" in text and "【酷狗音乐】" in text
    assert "3. 晴天 - 周杰伦" in text
    reply = group_event("3")
    assert run(plugin._is_picking(reply))
    assert not run(plugin._is_picking(group_event("3", user="43"))), "someone else's number is ignored"
    assert say(plugin.handle_pick, bot, reply, FakeMatcher()) is None
    card = bot.sent[-1][0]
    assert card.type == "music" and card.data["type"] == "163" and card.data["title"] == "晴天"
    assert not run(plugin._is_picking(group_event("3"))), "list is used up"


def test_song_command_usage_and_nothing_found(plugin, monkeypatch):
    assert "用法" in say(plugin.handle_song, FakeBot(), group_event("/点歌"), FakeMatcher(), (None,))
    empty = MusicService({"qq": FakeSource("qq", []), "kugou": FakeSource("kugou", [], error=SourceError("超时"))})
    monkeypatch.setattr(plugin, "music", empty)
    text = say(plugin.handle_song, FakeBot(), group_event("/点歌 x"), FakeMatcher(), ("x",))
    assert "都没搜到" in text and "酷狗音乐：超时" in text
    assert not run(plugin._is_picking(group_event("1")))


def test_song_command_respects_group_whitelist(plugin, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(plugin, "music_settings", SimpleNamespace(
        enabled=True, allowed_groups=["999"], allow_private=True, per_platform=3, cooldown=0))
    assert say(plugin.handle_song, FakeBot(), group_event("/点歌 晴天"), FakeMatcher(), ("晴天",)) is None


def test_card_falls_back_to_text(plugin):
    bot = FakeBot(card_fails=True)
    say(plugin.handle_song, bot, group_event("/点歌 晴天"), FakeMatcher(), ("晴天",))
    say(plugin.handle_pick, bot, group_event("3"), FakeMatcher())
    assert bot.sent[-1][0].type == "image" and "music.163.com" in str(bot.sent[-1])


def test_listen_plugin_is_gone():
    import importlib.util

    assert importlib.util.find_spec("mytest.plugins.listen_plugin") is None
