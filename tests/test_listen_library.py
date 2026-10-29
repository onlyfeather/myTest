"""一起听 M3：凭证加密、曲库存储、扫码导入、每日同步、bot 会员账号、WebUI 账号面板。

运行：python -m pytest tests/test_listen_library.py -q
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mytest.plugins._shared.settings import AccountPanel, SettingSection, SettingsStore
from mytest.plugins.listen_plugin.accounts.base import (
    AccountError,
    AccountInfo,
    CredentialExpired,
    QrStart,
    QrState,
    RemotePlaylist,
)
from mytest.plugins.listen_plugin.accounts.kugou import KugouAccounts
from mytest.plugins.listen_plugin.accounts.netease import NeteaseAccounts
from mytest.plugins.listen_plugin.app import create_app
from mytest.plugins.listen_plugin.crypto import CryptoError, Sealed, Vault, parse_key
from mytest.plugins.listen_plugin.library import (
    BOT,
    REFRESH_INTERVAL_MS,
    SYNC_INTERVAL_MS,
    Library,
    LibraryError,
)
from mytest.plugins.listen_plugin.service import ListenConfig, ListenService
from mytest.plugins.listen_plugin.sources.base import Stream, Track, VipCredential
from mytest.plugins.listen_plugin.store import Store
from mytest.plugins.webui_plugin.server import COOKIE_NAME, create_router

from .test_listen import GROUP, ORIGIN, SECRET, FakeBridge, login

KEY = base64.b64encode(os.urandom(32)).decode()
OTHER_KEY = base64.b64encode(os.urandom(32)).decode()
H = {"Origin": ORIGIN}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def song(n: int, *, vip: bool = False) -> Track:
    return Track("netease", str(1000 + n), f"歌{n}", "歌手", duration_ms=200_000, vip=vip)


# ---------------------------------------------------------------- 加密


def test_vault_roundtrip_and_tamper():
    vault = Vault(parse_key(KEY))
    sealed = vault.seal("123", "netease", {"cookie": "MUSIC_U=secret"})
    assert b"secret" not in sealed.ciphertext
    assert vault.open("123", "netease", sealed) == {"cookie": "MUSIC_U=secret"}
    with pytest.raises(CryptoError):
        vault.open("456", "netease", sealed)  # 把别人的密文挪到自己名下解不开
    with pytest.raises(CryptoError):
        vault.open("123", "qq", sealed)
    flipped = Sealed(bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:], sealed.nonce, sealed.key_id)
    with pytest.raises(CryptoError):
        vault.open("123", "netease", flipped)
    assert vault.seal("123", "netease", {"a": 1}).nonce != vault.seal("123", "netease", {"a": 1}).nonce


def test_vault_key_rotation():
    old = Vault(parse_key(KEY))
    sealed = old.seal("1", "qq", {"x": 1})
    new = Vault(parse_key(OTHER_KEY), old_key=parse_key(KEY))
    assert new.open("1", "qq", sealed) == {"x": 1}
    assert new.needs_rotation(sealed)
    with pytest.raises(CryptoError, match="重新扫码"):
        Vault(parse_key(OTHER_KEY)).open("1", "qq", sealed)  # 密钥丢了只能重新扫码


def test_vault_config_errors():
    assert Vault.from_env("") is None
    for bad in ("not base64!!", base64.b64encode(b"short").decode()):
        with pytest.raises(CryptoError):
            Vault.from_env(bad)


# ---------------------------------------------------------------- 假平台


class FakeAccounts:
    platform: str = "netease"
    login_kinds: tuple[str, ...] = ("app",)

    def __init__(self) -> None:
        self.polls = 0
        self.refreshes = 0
        self.expired = False
        self.remote = [
            RemotePlaylist("p1", "我喜欢", 2, liked=True),
            RemotePlaylist("p2", "私密的", 1, private=True),
            RemotePlaylist("p3", "公开的", 1),
        ]
        self.tracks_of = {"p1": [song(1), song(2)], "p2": [song(3)], "p3": [song(4)]}
        self.seen_credentials: list[dict | None] = []

    async def qr_start(self, kind: str) -> QrStart:
        self.polls = 0
        return QrStart({"key": "k"}, "data:image/png;base64,AAAA", "扫码")

    async def qr_poll(self, session: dict) -> QrState:
        self.polls += 1
        if self.polls < 2:
            return QrState("waiting")
        return QrState("done", credential={"cookie": "MUSIC_U=tok"})

    async def account(self, credential: dict) -> AccountInfo:
        if self.expired:
            raise CredentialExpired("失效了")
        return AccountInfo("777", "云村用户", vip=True)

    async def refresh(self, credential: dict) -> dict:
        self.refreshes += 1
        if self.expired:
            raise CredentialExpired("失效了")
        return {**credential, "refreshed": self.refreshes}

    async def playlists(self, *, credential: dict | None = None, account_id: str = "") -> list[RemotePlaylist]:
        self.seen_credentials.append(credential)
        if self.expired and credential:
            raise CredentialExpired("失效了")
        return [p for p in self.remote if credential or not p.private]

    async def tracks(self, playlist: RemotePlaylist, *, credential: dict | None = None, limit: int = 1000) -> list[Track]:
        return self.tracks_of[playlist.remote_id][:limit]


@pytest.fixture
def lib():
    clock = [10**12]
    store = Store(":memory:")
    acc = FakeAccounts()
    library = Library(store, {"netease": acc}, Vault(parse_key(KEY)), clock=lambda: clock[0])
    return library, store, acc, clock


async def scan(library: Library, owner: str) -> dict:
    sid, _ = await library.qr_start(owner, "netease", "app")
    first = await library.qr_poll(owner, sid)
    assert first["status"] == "waiting"
    return await library.qr_poll(owner, sid)


# ---------------------------------------------------------------- 扫码、存储


def test_qr_login_stores_only_ciphertext(lib):
    library, store, _, _ = lib
    done = run(scan(library, "123"))
    assert done["status"] == "done" and done["account"] == "云村用户"
    assert [p["remote_id"] for p in done["playlists"]] == ["p1", "p2", "p3"], "private ones are listed after login"
    assert "cookie" not in json.dumps(done) and "MUSIC_U" not in json.dumps(done)
    raw = store._db.execute("SELECT ciphertext, account_name FROM credentials").fetchall()
    assert len(raw) == 1 and b"MUSIC_U" not in raw[0][0] and raw[0][1] == "云村用户"
    status = library.credential_status("123")[0]
    assert status["logged_in"] and status["vip"] and "cookie" not in status


def test_qr_session_is_bound_to_owner(lib):
    library, *_ = lib

    async def go():
        sid, _ = await library.qr_start("123", "netease", "app")
        return await library.qr_poll("456", sid)

    assert run(go())["status"] == "expired", "another user can't poll my QR session"


def test_qr_needs_key():
    library = Library(Store(":memory:"), {"netease": FakeAccounts()}, None)
    with pytest.raises(LibraryError, match="LISTEN_CREDENTIAL_KEY"):
        run(library.qr_start("1", "netease", "app"))
    assert library.platforms()[0]["login_kinds"] == []


def test_import_after_login_keeps_private_hidden(lib):
    library, store, _, _ = lib
    run(scan(library, "123"))
    imported = run(library.import_playlists("123", "netease", ["p1", "p2", "nope"], via="login"))
    assert [i["tracks"] for i in imported] == [2, 1]
    mine = store.playlists("123", include_hidden=True)
    assert [(p["name"], p["hidden"]) for p in mine] == [("我喜欢", False), ("私密的", True)]
    assert [p["name"] for p in library.overview("123", viewer="456")["playlists"]] == ["我喜欢"]
    hidden_id = next(p["id"] for p in mine if p["hidden"])
    assert library.playlist(hidden_id, viewer="456") is None
    assert library.playlist(hidden_id, viewer="123")["tracks"][0]["title"] == "歌3"


def test_import_by_uid_only_public(lib):
    library, store, acc, _ = lib
    listed = run(library.list_by_uid("123", "netease", "777"))
    assert [p["remote_id"] for p in listed] == ["p1", "p3"]
    run(library.import_playlists("123", "netease", ["p2", "p3"], via="uid"))
    assert [p["name"] for p in store.playlists("123", include_hidden=True)] == ["公开的"]
    assert all(c is None for c in acc.seen_credentials), "uid import never uses a credential"
    with pytest.raises(LibraryError):
        run(library.list_by_uid("123", "netease", "12a"))


def test_hidden_setting_survives_sync(lib):
    library, store, _, clock = lib
    run(scan(library, "123"))
    run(library.import_playlists("123", "netease", ["p2"], via="login"))
    pid = store.playlists("123", include_hidden=True)[0]["id"]
    store.set_playlist_hidden("123", pid, False)
    clock[0] += SYNC_INTERVAL_MS + 1

    async def go():
        import mytest.plugins.listen_plugin.library as mod
        original = mod.asyncio.sleep

        async def fast(_: float) -> None:
            await original(0)

        mod.asyncio.sleep = fast
        try:
            return await library.run_daily()
        finally:
            mod.asyncio.sleep = original

    stats = run(go())
    assert stats["synced"] == 1
    assert store.playlists("123", include_hidden=True)[0]["hidden"] is False, "user's visibility choice is kept"


def daily(library: Library) -> dict:
    import mytest.plugins.listen_plugin.library as mod

    async def go():
        original = mod.asyncio.sleep

        async def fast(_: float) -> None:
            await original(0)

        mod.asyncio.sleep = fast
        try:
            return await library.run_daily()
        finally:
            mod.asyncio.sleep = original

    return run(go())


def test_daily_refresh_and_expiry(lib):
    library, store, acc, clock = lib
    run(scan(library, "123"))
    run(library.import_playlists("123", "netease", ["p1"], via="login"))
    assert daily(library)["refreshed"] == 0, "fresh credentials are not refreshed again"
    clock[0] += REFRESH_INTERVAL_MS + 1
    acc.tracks_of["p1"] = [song(1), song(2), song(9)]
    clock[0] += SYNC_INTERVAL_MS
    stats = daily(library)
    assert stats["refreshed"] == 1 and stats["synced"] == 1
    loaded = library._load("123", "netease")
    assert loaded and loaded[0]["refreshed"] == 1, "refreshed credential is re-encrypted and saved"
    assert library.playlist(store.playlists("123", include_hidden=True)[0]["id"], viewer="123")["tracks"][-1][
        "title"] == "歌9"

    acc.expired = True
    clock[0] += SYNC_INTERVAL_MS + REFRESH_INTERVAL_MS
    stats = daily(library)
    assert stats["expired"] == 1
    status = library.credential_status("123")[0]
    assert status["expired"] and not status["logged_in"]
    assert "重新扫码" in store.playlists("123", include_hidden=True)[0]["sync_error"]


def test_logout_and_forget_delete_credentials(lib):
    library, store, _, _ = lib
    run(scan(library, "123"))
    run(library.import_playlists("123", "netease", ["p1"], via="login"))
    library.logout("123", "netease")
    assert store.credential("123", "netease") is None
    assert store.playlists("123", include_hidden=True), "playlists are kept after unbinding"
    run(scan(library, "123"))
    library.forget_user("123")
    assert store.credential("123", "netease") is None


def test_favorites(lib):
    library, store, _, _ = lib
    assert library.like("1", song(1))
    assert not library.like("1", song(1)), "already liked"
    library.like("1", song(2))
    assert library.liked_refs("1") == {song(1).ref, song(2).ref}
    assert {t["title"] for t in library.overview("1", viewer="2")["favorites"]["tracks"]} == {"歌1", "歌2"}
    library.unlike("1", song(1).ref)
    assert library.liked_refs("1") == {song(2).ref}


def test_corrupt_credential_is_marked_not_crashing(lib):
    library, store, _, _ = lib
    run(scan(library, "123"))
    store._db.execute("UPDATE credentials SET ciphertext = x'00'")
    store._db.commit()
    assert library._load("123", "netease") is None
    assert library.credential_status("123")[0]["expired"]


def test_old_database_gets_new_tables(tmp_path):
    path = tmp_path / "listen.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, qq TEXT, group_id TEXT, expires_at INTEGER);")
    db.close()
    store = Store(path)
    assert store.favorites("1") == [] and store.playlists("1", include_hidden=True) == []


# ---------------------------------------------------------------- bot 会员账号


class VipSource(VipCredential):
    platform = "netease"

    def __init__(self) -> None:
        super().__init__()
        self.used: list[dict | None] = []

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        return [song(1, vip=True)]

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        cred = self.credential()
        self.used.append(cred)
        if track.vip and not cred:
            from mytest.plugins.listen_plugin.sources.base import SourceError

            raise SourceError("会员歌")
        return Stream(f"https://cdn/{track.id}.mp3", 600, bitrate)

    async def lyrics(self, track: Track) -> tuple[str, str]:
        return "", ""


def test_bot_credential_unlocks_vip(lib):
    library, store, acc, _ = lib
    source = VipSource()
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=FakeBridge(), store=store, source_map={"netease": source}, library=library,
    )
    assert not service.vip_ready("netease")
    run(scan(library, BOT))
    assert service.vip_ready("netease")
    stream = run(service.stream(song(1, vip=True), 320))
    assert stream.url.endswith(".mp3") and source.used[-1] == {"cookie": "MUSIC_U=tok"}
    assert store.credential(BOT, "netease") is not None and store.linked_account(BOT, "netease") is None, \
        "bot login doesn't create a linked account or list playlists"
    source.credential_failed("会员过期了")
    assert not service.vip_ready("netease")
    assert library.credential_status(BOT)[0]["expired"]


def test_vip_cache_cleared_on_logout(lib):
    library, store, _, _ = lib
    source = VipSource()
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=FakeBridge(), store=store, source_map={"netease": source}, library=library,
    )
    run(scan(library, BOT))
    run(service.stream(song(1, vip=True), 320))
    library.logout(BOT, "netease")
    from mytest.plugins.listen_plugin.sources.base import SourceError

    with pytest.raises(SourceError):
        run(service.stream(song(1, vip=True), 320))  # 已缓存的会员播放地址也作废


def test_member_who_left_all_groups_loses_credentials(lib):
    library, store, _, _ = lib
    bridge = FakeBridge()
    bridge.add("123", "群友")
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=bridge, store=store, source_map={}, library=library,
    )
    run(scan(library, "123"))
    del bridge.members[(GROUP, "123")]
    run(service.member_left(GROUP, "123"))
    assert store.credential("123", "netease") is None
    run(scan(library, BOT))
    run(service.enforce_whitelist())
    assert store.credential(BOT, "netease") is not None, "bot credentials are never dropped"


# ---------------------------------------------------------------- 一起听页面接口


@pytest.fixture
def web(lib):
    library, store, acc, _ = lib
    bridge = FakeBridge()
    bridge.add("1", "房主")
    bridge.add("2", "群友")
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=bridge, store=store, source_map={"netease": VipSource()}, library=library,
    )
    client = TestClient(create_app(service), base_url=ORIGIN)
    return service, client, library, store, acc


def test_web_qr_import_and_visibility(web):
    service, client, library, store, _ = web
    login(client, service, "2")
    acc = client.get("/api/library/accounts").json()
    assert acc["enabled"] and acc["platforms"][0]["login_kinds"] == ["app"]
    start = client.post("/api/library/qr", json={"platform": "netease"}, headers=H).json()
    assert start["image"].startswith("data:image/") and "session" not in start
    assert client.get(f"/api/library/qr/{start['sid']}").json()["status"] == "waiting"
    done = client.get(f"/api/library/qr/{start['sid']}").json()
    assert done["status"] == "done" and len(done["playlists"]) == 3
    r = client.post("/api/library/import", json={"platform": "netease", "via": "login", "remote_ids": ["p1", "p2"]},
                    headers=H).json()
    assert len(r["imported"]) == 2
    mine = client.get("/api/library/2").json()
    assert mine["mine"] and len(mine["playlists"]) == 2
    hidden = next(p for p in mine["playlists"] if p["hidden"])

    client.cookies.clear()
    login(client, service, "1")
    theirs = client.get("/api/library/2").json()
    assert [p["name"] for p in theirs["playlists"]] == ["我喜欢"]
    assert client.get(f"/api/playlists/{hidden['id']}").status_code == 404
    shown = theirs["playlists"][0]
    detail = client.get(f"/api/playlists/{shown['id']}").json()
    assert [t["title"] for t in detail["tracks"]] == ["歌1", "歌2"] and not detail["mine"]
    assert client.post(f"/api/playlists/{shown['id']}/visibility", json={"hidden": True}, headers=H).status_code == 404, \
        "can't change someone else's playlist"
    assert client.get("/api/library/404").status_code == 404, "not in the group"


def test_web_favorites(web):
    service, client, *_ = web
    login(client, service, "2")
    ref = song(1, vip=True).ref
    assert client.post("/api/favorites", json={"ref": ref}, headers=H).status_code == 404, "unknown track"
    service.remember([song(1, vip=True)])
    assert client.post("/api/favorites", json={"ref": ref}, headers=H).json()["added"]
    assert client.get("/api/favorites/refs").json()["refs"] == [ref]
    assert client.get("/api/library/2").json()["favorites"]["count"] == 1
    client.post("/api/favorites/remove", json={"ref": ref}, headers=H)
    assert client.get("/api/favorites/refs").json()["refs"] == []
    assert client.post("/api/favorites", json={"ref": ref}, headers={"Origin": "https://evil"}).status_code == 403


def test_web_responses_never_leak_credentials(web):
    service, client, library, store, _ = web
    login(client, service, "2")
    start = client.post("/api/library/qr", json={"platform": "netease"}, headers=H).json()
    client.get(f"/api/library/qr/{start['sid']}")
    client.get(f"/api/library/qr/{start['sid']}")
    for path in ("/api/library/accounts", "/api/library/2", "/api/me"):
        assert "MUSIC_U" not in client.get(path).text, path


# ---------------------------------------------------------------- WebUI 账号面板


def test_webui_account_panel(tmp_path, lib):
    library, *_ = lib
    store = SettingsStore(tmp_path / "s.json")
    store.register(SettingSection(id="listen", title="一起听"))

    async def qr_start(platform: str, kind: str) -> dict:
        sid, start = await library.qr_start(BOT, platform, kind)
        return {"sid": sid, "image": start.image, "hint": start.hint, "session": "SHOULD NOT LEAK"}

    async def qr_poll(sid: str) -> dict:
        return await library.qr_poll(BOT, sid)

    store.set_account_panel("listen", AccountPanel(
        title="bot", help="", status=lambda: [{**r, "login_kinds": ["app"], "cookie": "LEAK"}
                                               for r in library.credential_status(BOT)],
        qr_start=qr_start, qr_poll=qr_poll, logout=lambda p: library.logout(BOT, p),
    ))
    app = FastAPI()
    app.include_router(create_router(store, lambda: "x" * 16))
    client = TestClient(app)
    headers = {"X-Requested-With": "mytest-webui"}
    assert client.post("/webui/api/accounts/listen/netease/qr", headers=headers).status_code == 401
    client.post("/webui/api/login", json={"password": "x" * 16}, headers=headers)
    assert client.cookies.get(COOKIE_NAME)
    desc = client.get("/webui/api/settings").json()["sections"][0]["accounts"]
    assert desc["items"][0]["logged_in"] is False and "cookie" not in desc["items"][0]
    assert client.post("/webui/api/accounts/listen/netease/qr").status_code == 403, "needs X-Requested-With"
    start = client.post("/webui/api/accounts/listen/netease/qr", json={"kind": "app"}, headers=headers).json()
    assert start["image"] and "session" not in start
    client.post(f"/webui/api/accounts/listen/qr/{start['sid']}", headers=headers)
    done = client.post(f"/webui/api/accounts/listen/qr/{start['sid']}", headers=headers).json()
    assert done["status"] == "done" and done["vip"] is True and "playlists" not in done
    assert client.get("/webui/api/settings").json()["sections"][0]["accounts"]["items"][0]["logged_in"]
    client.post("/webui/api/accounts/listen/netease/logout", headers=headers)
    assert not client.get("/webui/api/settings").json()["sections"][0]["accounts"]["items"][0]["logged_in"]


# ---------------------------------------------------------------- 平台账号（假 HTTP）


def ncm_mock(routes: dict[str, Any], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = routes.get(request.url.path)
        body = body(request) if callable(body) else body
        return httpx.Response(200, json=body if body is not None else {"code": 404})

    return httpx.MockTransport(handler)


def test_netease_accounts_keep_cookie_out_of_url():
    seen: list[httpx.Request] = []
    routes = {
        "/user/account": {"code": 200, "account": {"id": 9, "vipType": 11}, "profile": {"userId": 9, "nickname": "我"}},
        "/user/playlist": {"code": 200, "more": False, "playlist": [
            {"id": 1, "name": "我喜欢的音乐", "trackCount": 2, "privacy": 0, "specialType": 5, "creator": {"userId": 9}},
            {"id": 2, "name": "私密", "trackCount": 1, "privacy": 10, "specialType": 0, "creator": {"userId": 9}},
            {"id": 3, "name": "收藏别人的", "trackCount": 9, "privacy": 0, "specialType": 0, "creator": {"userId": 8}},
        ]},
        "/playlist/track/all": {"code": 200, "songs": [
            {"id": 5, "name": "a", "ar": [{"name": "x"}], "al": {"picUrl": "http://p/a.jpg"}, "dt": 1000, "fee": 1}]},
        "/login/qr/check": {"code": 803, "cookie": "MUSIC_U=abc; __csrf=1"},
    }
    acc = NeteaseAccounts("http://ncm", transport=ncm_mock(routes, seen))
    cred = {"cookie": "MUSIC_U=abc"}
    info = run(acc.account(cred))
    assert info == AccountInfo("9", "我", vip=True)
    pls = run(acc.playlists(credential=cred))
    assert [(p.remote_id, p.private, p.liked) for p in pls] == [("1", False, True), ("2", True, False)]
    tracks = run(acc.tracks(pls[0], credential=cred))
    assert tracks[0].vip and tracks[0].cover.startswith("https://")
    assert run(acc.qr_poll({"key": "k"})).credential == {"cookie": "MUSIC_U=abc; __csrf=1"}
    for r in seen:
        assert "MUSIC_U" not in str(r.url), r.url
        assert "nonce" in r.url.params, "cache-busting nonce in the URL"
    assert any(b"MUSIC_U" in r.content for r in seen), "cookie travels in the body"
    urls = {str(r.url) for r in seen}
    assert len(urls) == len(seen), "every request URL is unique (bypasses the 2-minute cache)"


def test_netease_expired_and_uid_errors():
    seen: list[httpx.Request] = []
    acc = NeteaseAccounts("http://ncm", transport=ncm_mock({"/user/account": {"code": 301}}, seen))
    with pytest.raises(CredentialExpired):
        run(acc.account({"cookie": "x"}))
    acc = NeteaseAccounts("http://ncm", transport=ncm_mock({
        "/user/playlist": {"code": 200, "more": False, "playlist": []}, "/user/detail": {"code": 404}}, seen))
    with pytest.raises(AccountError, match="没有这个"):
        run(acc.playlists(account_id="999"))
    with pytest.raises(AccountError):
        run(acc.playlists(account_id="abc"))


def test_kugou_accounts():
    seen: list[httpx.Request] = []
    routes = {
        "/login/qr/key": {"data": {"qrcode": "Q"}},
        "/login/qr/create": {"data": {"base64": "data:image/png;base64,AA"}},
        "/register/dev": {"data": {"dfid": "D"}},
        "/login/qr/check": lambda r: {"data": {"status": 4, "token": "T", "userid": "42"}},
        "/user/detail": {"status": 1, "data": {"nickname": "酷狗用户"}},
        "/user/vip/detail": {"data": {"busi_vip": [{"is_vip": 1}]}},
        "/user/playlist": {"data": {"info": [
            {"listid": 3, "global_collection_id": "c3", "name": "我喜欢", "count": 1, "is_def": 2,
             "list_create_userid": 42},
            {"listid": 4, "global_collection_id": "c4", "name": "别人的", "count": 5, "list_create_userid": 7},
        ]}},
        "/playlist/track/all/new": {"data": {"songs": [
            {"hash": "a" * 32, "name": "歌手甲 - 歌名", "timelen": 1000, "mixsongid": 9, "album_id": "0",
             "relate_goods": [{"privilege": 10}]}]}},
        "/login/token": {"status": 1, "data": {"token": "T2"}},
    }
    acc = KugouAccounts("http://kg", transport=ncm_mock(routes, seen))
    start = run(acc.qr_start())
    assert start.session == {"key": "Q", "dfid": "D"}
    cred = run(acc.qr_poll(start.session)).credential
    assert cred == {"token": "T", "userid": "42", "dfid": "D"}
    assert run(acc.account(cred)) == AccountInfo("42", "酷狗用户", vip=True)
    pls = run(acc.playlists(credential=cred))
    assert [(p.remote_id, p.liked) for p in pls] == [("3", True)], "only own playlists"
    t = run(acc.tracks(pls[0], credential=cred))[0]
    assert (t.title, t.artists, t.vip, t.file_id) == ("歌名", "歌手甲", True, "0_9")
    assert run(acc.refresh(cred))["token"] == "T2"
    with pytest.raises(AccountError):
        run(acc.playlists(account_id="42"))  # 酷狗没有公开歌单接口
    for r in seen:
        assert "cookie" not in r.url.params and "T" not in str(r.url.params.get("token", "")), r.url
    logged_in = [r for r in seen if "authorization" in r.headers]
    assert logged_in and all(r.url.path not in ("/login/qr/key", "/login/qr/check") for r in logged_in)
    assert kugou_server_cookie(logged_in[0]) == {"token": "T", "userid": "42", "dfid": "D"}


def kugou_server_cookie(request: httpx.Request) -> dict[str, str]:
    """照搬 KuGouMusicApi 的解析（server.js + util.cookieToJson）：按 ";" 切、按 "=" 切，不去空格。

    实测（2026-10-29，KuGouMusicApi 1.6.3）：请求体里的 cookie 会替换掉服务注入的设备 cookie，
    "token=T; userid=42" 解析成 {"token": "T", " userid": "42"}，userid 读不到，酷狗回 20018。
    """
    out: dict[str, str] = {}
    for part in request.headers.get("authorization", "").split(";"):
        key, _, value = part.partition("=")
        out[key] = value
    return out


def test_kugou_auth_header_has_no_spaces():
    from mytest.plugins.listen_plugin.accounts.kugou import auth_header

    assert auth_header({"token": "T", "userid": "42", "dfid": "D"}) == {"Authorization": "token=T;userid=42;dfid=D"}
    assert auth_header({"token": "T", "userid": "42", "dfid": ""}) == {"Authorization": "token=T;userid=42"}
    assert auth_header({}) == {}


def test_kugou_vip_stream_sends_login_in_header():
    from mytest.plugins.listen_plugin.sources.base import Track
    from mytest.plugins.listen_plugin.sources.kugou import KugouSource

    seen: list[httpx.Request] = []
    routes = {"/song/url": {"status": 1, "url": ["http://fs.kugou.com/vip.mp3"]}}
    src = KugouSource("http://kg", transport=ncm_mock(routes, seen))
    src.bind_credential(lambda: {"token": "T", "userid": "42", "dfid": "D"}, lambda message: None)
    track = Track("kugou", "B" * 32, "会员歌", "歌手", duration_ms=1, vip=True, file_id="1_2")
    assert run(src.stream(track, 320)).url == "https://fs.kugou.com/vip.mp3"
    assert kugou_server_cookie(seen[-1]) == {"token": "T", "userid": "42", "dfid": "D"}
    assert "cookie" not in seen[-1].url.params and seen[-1].content == b""


# ---------------------------------------------------------------- QQ 音乐扫码


def test_qq_cookie_snapshot_survives_empty_cookie():
    """qqmusic-api-python 0.8.2：check_sig 响应里有空值 cookie 时，原函数抛 KeyError。"""
    pytest.importorskip("qqmusic_api")
    from niquests.cookies import RequestsCookieJar
    from qqmusic_api.core import executor, response

    from mytest.plugins.listen_plugin.accounts.qqmusic import patch_cookie_snapshot

    jar = RequestsCookieJar()
    jar.set("p_skey", "abc", domain=".qq.com")
    jar.set("pt_recent_uins", "", domain=".ptlogin2.qq.com")

    class Resp:
        status_code, url, headers, content, text = 200, "https://ssl.ptlogin2.graph.qq.com/check_sig", {}, b"", ""
        cookies = jar

    patch_cookie_snapshot()
    for fn in (response.snapshot_payload, executor.snapshot_payload):
        payload = fn(Resp())
        assert payload.cookies == {"p_skey": "abc", "pt_recent_uins": ""}
        assert payload.cookies.get("p_skey") == "abc"


class _FakeQr:
    data, mimetype, identifier = b"png", "image/png", "qr-id"


def _qq_accounts_with(monkeypatch, events):
    """把 QQ 音乐库换成假的：get_qrcode 给二维码，checking_mobile_qrcode 依次推送 events。"""
    pytest.importorskip("qqmusic_api")
    from qqmusic_api.models.login import QRLoginType

    from mytest.plugins.listen_plugin.accounts import qqmusic

    asked: list[Any] = []

    class Login:
        async def get_qrcode(self, login_type):
            asked.append(login_type)
            return _FakeQr()

        async def checking_mobile_qrcode(self, qr, deadline=None):
            for event in events:
                if isinstance(event, BaseException):
                    raise event
                await asyncio.sleep(0)
                yield event

    class Client:
        login = Login()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    acc = qqmusic.QQMusicAccounts()
    monkeypatch.setattr(acc, "_client", lambda credential=None: Client())
    return acc, asked, QRLoginType


def _result(event, credential=None):
    from types import SimpleNamespace

    return SimpleNamespace(event=event, credential=credential)


def test_qq_app_scan_flow(monkeypatch):
    pytest.importorskip("qqmusic_api")
    from qqmusic_api import Credential
    from qqmusic_api.models.login import QRCodeLoginEvents as E

    cred = Credential(musicid=42, musickey="Q_H_L_key", login_type=2)
    acc, asked, types_ = _qq_accounts_with(monkeypatch, [_result(E.SCAN), _result(E.CONF), _result(E.DONE, cred)])
    assert acc.login_kinds[0] == "app", "QQ 音乐 App 扫码排第一"

    async def go():
        start = await acc.qr_start("app")
        assert start.image.startswith("data:image/png;base64,") and "QQ 音乐 App" in start.hint
        states = []
        for _ in range(20):
            state = await acc.qr_poll(start.session)
            states.append(state.status)
            if state.status == "done":
                return states, state
            await asyncio.sleep(0)
        raise AssertionError(states)

    states, done = run(go())
    assert asked == [types_.MOBILE]
    assert done.credential["musicid"] == 42 and done.credential["musickey"] == "Q_H_L_key"
    assert states[-1] == "done"
    assert not acc._app, "finished scans are dropped"


def test_qq_app_scan_refused_and_errors(monkeypatch):
    pytest.importorskip("qqmusic_api")
    from qqmusic_api import LoginError
    from qqmusic_api.models.login import QRCodeLoginEvents as E

    async def until_final(acc, session):
        for _ in range(20):
            await asyncio.sleep(0)
            state = await acc.qr_poll(session)
            if state.status not in ("waiting", "scanned"):
                return state.status
        raise AssertionError("never finished")

    acc, _, _ = _qq_accounts_with(monkeypatch, [_result(E.SCAN), _result(E.REFUSE)])
    assert run(_run_refused(acc, until_final)) == "refused"

    acc2, _, _ = _qq_accounts_with(monkeypatch, [_result(E.SCAN), LoginError("账号受限", code=20277)])

    async def failing():
        start = await acc2.qr_start("app")
        for _ in range(20):
            await asyncio.sleep(0)
            await acc2.qr_poll(start.session)
        raise AssertionError("no error")

    with pytest.raises(AccountError, match="QQ 音乐登录失败"):
        run(failing())

    acc3, _, _ = _qq_accounts_with(monkeypatch, [_result(E.SCAN)])  # 推送结束还没扫：算过期
    assert run(_run_refused(acc3, until_final)) == "expired"


async def _run_refused(acc, until_final):
    start = await acc.qr_start("app")
    return await until_final(acc, start.session)


def test_qq_web_kinds_still_poll(monkeypatch):
    pytest.importorskip("qqmusic_api")
    from qqmusic_api.models.login import QRCodeLoginEvents as E

    acc, asked, types_ = _qq_accounts_with(monkeypatch, [])

    async def check_qrcode(qr):
        return _result(E.SCAN)

    acc._client().login.check_qrcode = check_qrcode

    async def go():
        start = await acc.qr_start("qq")
        assert "手机 QQ" in start.hint and "扫不了" in start.hint
        return (await acc.qr_poll(start.session)).status

    assert run(go()) == "waiting"
    assert asked == [types_.QQ]


def test_qq_close_cancels_app_watchers(monkeypatch):
    pytest.importorskip("qqmusic_api")
    from qqmusic_api.models.login import QRCodeLoginEvents as E

    acc, _, _ = _qq_accounts_with(monkeypatch, [_result(E.SCAN)] + [_result(E.SCAN)] * 1000)

    async def go():
        await acc.qr_start("app")
        task = next(iter(acc._app.values())).task
        await acc.close()
        await asyncio.sleep(0)
        return task

    task = run(go())
    assert task.done()
