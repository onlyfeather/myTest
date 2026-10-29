"""一起听：房间状态机、同步、登录、WebSocket。运行：python -m pytest tests/test_listen.py -q"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mytest.plugins.listen_plugin.app import (
    PingMsg,
    client_ip,
    create_app,
    parse_message,
)
from mytest.plugins.listen_plugin.auth import (
    AuthError,
    CodeBook,
    Identity,
    LinkSigner,
    Sessions,
)
from mytest.plugins.listen_plugin.bridge import BotUnavailable, Member, MembershipCache
from mytest.plugins.listen_plugin.hub import IDLE_CLOSE_MS, Hub
from mytest.plugins.listen_plugin.room import CONTROL_LIMIT, Room, RoomError
from mytest.plugins.listen_plugin.service import ListenConfig, ListenService
from mytest.plugins.listen_plugin.sources.base import (
    SourceError,
    Stream,
    Track,
    parse_ref,
)
from mytest.plugins.listen_plugin.store import Store
from mytest.plugins.listen_plugin.sync import Anchor, correction

SECRET = "s" * 40
ORIGIN = "https://listen.example.com"
GROUP = "100001"


def make_track(n: int = 1, duration_ms: int = 200_000) -> Track:
    return Track(platform="qq", id=f"00{n:010d}", title=f"歌{n}", artists="歌手", duration_ms=duration_ms)


# ---------------------------------------------------------------- 同步


def test_anchor_position():
    a = Anchor(server_ms=1000, position_ms=5000, paused=False)
    assert a.position_at(3000) == 7000
    assert a.position_at(500) == 5000, "server clock never goes backwards past the anchor"
    assert Anchor(1000, 5000, paused=True).position_at(99999) == 5000


def test_correction_thresholds():
    assert correction(10_100, 10_000) == ("none", 1.0)
    assert correction(11_000, 10_000) == ("rate", 0.95), "ahead -> slow down"
    assert correction(9_000, 10_000) == ("rate", 1.05), "behind -> speed up"
    assert correction(13_000, 10_000)[0] == "seek"


# ---------------------------------------------------------------- 房间


def test_room_play_pause_seek_next():
    room = Room(group_id=GROUP, owner="1")
    with pytest.raises(RoomError):
        room.play("1", 0)
    room.add(make_track(1), "2", "B")
    room.add(make_track(2), "2", "B")
    room.play("2", 1000)
    assert room.current and room.current.track.id == make_track(1).id
    assert not room.anchor.paused and room.position_at(4000) == 3000
    room.pause("2", 4000)
    assert room.anchor.paused and room.position_at(99_000) == 3000
    room.seek("2", 5000, 60_000)
    assert room.position_at(5000) == 60_000
    room.seek("1", 6000, 10**9)
    assert room.position_at(6000) == 199_000, "seek clamps to just before the end"
    room.next("1", 7000)
    assert room.current is not None and room.current.track.id == make_track(2).id
    assert room.position_at(7000) == 0
    assert [t.id for t in room.history] == [make_track(1).id]


def test_room_stale_version_is_rejected():
    room = Room(group_id=GROUP, owner="1")
    room.add(make_track(1), "1", "A")
    room.play("1", 0)
    seen = room.version
    room.pause("1", 1000, version=seen)
    with pytest.raises(RoomError):
        room.play("2", 2000, version=seen)
    assert room.anchor.paused


def test_room_permission_and_rate_limit():
    room = Room(group_id=GROUP, owner="1")
    for n in range(5):
        room.add(make_track(n), "1", "A")
    room.play("1", 0)
    room.set_allow_control(False, "1")
    with pytest.raises(RoomError):
        room.pause("2", 100)
    room.pause("9", 100, superuser=True)
    with pytest.raises(RoomError):
        room.set_allow_control(True, "2")
    room.set_allow_control(True, "1")
    t = 1000
    for _ in range(CONTROL_LIMIT):
        room.seek("2", t, 1000)
        t += 100
    with pytest.raises(RoomError):
        room.seek("2", t, 1000)
    room.seek("2", t + 10_000, 1000)
    for _ in range(10):
        room.seek("1", t, 1000)  # 房主不限速


def test_room_queue_ownership():
    room = Room(group_id=GROUP, owner="1", allow_control=False)
    a = room.add(make_track(1), "2", "B")
    b = room.add(make_track(2), "3", "C")
    with pytest.raises(RoomError):
        room.remove(b.item_id, "2")
    room.remove(a.item_id, "2")
    with pytest.raises(RoomError):
        room.move(b.item_id, 0, "3")
    with pytest.raises(RoomError):
        room.remove(999, "1")


def test_room_auto_advance_and_restore():
    room = Room(group_id=GROUP, owner="1")
    room.add(make_track(1, duration_ms=10_000), "1", "A")
    room.add(make_track(2), "1", "A")
    room.play("1", 0)
    assert not room.auto_advance(9_000)
    assert room.auto_advance(10_000)
    assert room.current is not None and room.current.track.id == make_track(2).id
    room.pause("1", 15_000)
    assert not room.auto_advance(10**9), "paused never finishes"
    restored = Room.restore(room.snapshot(20_000), now=50_000)
    assert restored.current is not None and restored.current.track == make_track(2)
    assert restored.anchor.paused and restored.position_at(50_000) == 5_000
    assert [t.id for t in restored.history] == [make_track(1).id]


# ---------------------------------------------------------------- 歌曲引用


def test_track_ref_validation():
    assert parse_ref("qq:0001OKOk2tCZb8") == ("qq", "0001OKOk2tCZb8")
    for bad in ("", "qq:", "spotify:1", "qq:../etc", "qq:a b", "qq:" + "a" * 65):
        with pytest.raises(SourceError):
            parse_ref(bad)
    t = Track.from_dict({"platform": "qq", "id": "abc", "title": "x", "cover": "javascript:alert(1)",
                         "file_id": "../x"})
    assert t.cover == "" and t.file_id == ""


# ---------------------------------------------------------------- 登录


def test_link_signer():
    signer = LinkSigner(SECRET)
    token = signer.issue("123", GROUP, now=0)
    identity, jti, _ = signer.verify(token, now=1000)
    assert identity == Identity("123", GROUP) and jti
    with pytest.raises(AuthError):
        signer.verify(token, now=11 * 60 * 1000)
    body, sig = token.split(".")
    with pytest.raises(AuthError):
        signer.verify(body[:-2] + "AA." + sig, now=0)
    with pytest.raises(AuthError):
        LinkSigner("x" * 40).verify(token, now=0)
    with pytest.raises(AuthError):
        signer.verify("garbage", now=0)
    with pytest.raises(ValueError):
        LinkSigner("short")


def test_link_is_single_use():
    store = Store(":memory:")
    sessions = Sessions(store)
    signer = LinkSigner(SECRET)
    token = signer.issue("123", GROUP, now=0)
    assert sessions.consume_link(signer, token, now=10) == Identity("123", GROUP)
    with pytest.raises(AuthError):
        sessions.consume_link(signer, token, now=20)


def test_sessions():
    store = Store(":memory:")
    sessions = Sessions(store)
    a = sessions.create(Identity("1", GROUP), now=0)
    b = sessions.create(Identity("2", GROUP), now=0)
    assert sessions.get(a, now=1) == Identity("1", GROUP)
    assert sessions.get("nope", now=1) is None
    assert sessions.get(a, now=31 * 24 * 3600 * 1000) is None, "expired"
    sessions.revoke_user("1", GROUP)
    assert sessions.get(a, now=1) is None and sessions.get(b, now=1)
    store.delete_sessions_outside(["999"])
    assert sessions.get(b, now=1) is None


def test_code_book():
    book = CodeBook()
    code = book.create("browser-1", now=0)
    assert len(code) == 6 and code.isdigit()
    assert book.poll("browser-1", now=1) is None
    assert not book.redeem("000000" if code != "000000" else "111111", Identity("1", GROUP), now=2)
    assert book.redeem(code, Identity("1", GROUP), now=3)
    assert not book.redeem(code, Identity("2", GROUP), now=4), "first redeemer wins"
    assert book.poll("browser-1", now=5) == Identity("1", GROUP)
    assert book.poll("browser-1", now=6) is None, "code is consumed"
    late = book.create("browser-2", now=0)
    assert not book.redeem(late, Identity("1", GROUP), now=6 * 60 * 1000), "expired"


def test_code_book_rate_limit():
    book = CodeBook()
    for i in range(5):
        book.redeem(f"{i:06d}", Identity("1", GROUP), now=i)
    with pytest.raises(AuthError):
        book.redeem("123456", Identity("1", GROUP), now=10)
    book.redeem("123456", Identity("1", GROUP), now=61_000)


def test_client_ip_only_trusts_proxies():
    trusted = ("127.0.0.1",)
    assert client_ip("1.2.3.4", "9.9.9.9", trusted) == "1.2.3.4", "untrusted peer can't spoof"
    assert client_ip("127.0.0.1", "9.9.9.9", trusted) == "9.9.9.9"
    assert client_ip("127.0.0.1", "8.8.8.8, 9.9.9.9, 127.0.0.1", trusted) == "9.9.9.9"
    assert client_ip("127.0.0.1", "not-an-ip", trusted) == "127.0.0.1"


def test_parse_message():
    assert isinstance(parse_message('{"type":"ping","client_ms":1}'), PingMsg)
    for bad in ('[]', '{"type":"x"}', '{"type":"chat","text":""}', '{"type":"chat","text":"' + "a" * 201 + '"}',
                '{"type":"control","action":"explode"}', "x" * 5000):
        with pytest.raises(ValueError):
            parse_message(bad)


# ---------------------------------------------------------------- 成员缓存


class FakeBridge:
    def __init__(self) -> None:
        self.members: dict[tuple[str, str], Member] = {}
        self.calls = 0
        self.offline = False
        self.superusers = {"999"}

    def add(self, qq: str, name: str, group_id: str = GROUP) -> None:
        self.members[(group_id, qq)] = Member(qq=qq, group_id=group_id, nickname=name)

    async def member(self, group_id: str, qq: str) -> Member | None:
        self.calls += 1
        if self.offline:
            raise BotUnavailable
        return self.members.get((group_id, qq))

    def is_superuser(self, qq: str) -> bool:
        return qq in self.superusers


def test_membership_cache():
    bridge = FakeBridge()
    bridge.add("1", "A")
    clock = [0.0]
    cache = MembershipCache(bridge, clock=lambda: clock[0])

    async def run():
        found = await cache.member(GROUP, "1")
        assert found is not None and found.nickname == "A"
        assert await cache.member(GROUP, "1")
        assert bridge.calls == 1
        assert await cache.member(GROUP, "2") is None
        clock[0] = 61
        await cache.member(GROUP, "2")
        assert bridge.calls == 3, "negative results expire after a minute"
        cache.forget(GROUP, "1")
        await cache.member(GROUP, "1")
        assert bridge.calls == 4

    asyncio.run(run())


# ---------------------------------------------------------------- Hub


def test_hub_reaps_idle_rooms():
    store = Store(":memory:")
    clock = [0]
    hub = Hub(store, clock=lambda: clock[0])
    room, created = hub.open(GROUP, "1")
    assert created and hub.open(GROUP, "2") == (room, False)
    room.add(make_track(1), "1", "A")
    hub.save(GROUP)

    async def run():
        clock[0] = IDLE_CLOSE_MS - 1
        await hub.reap()
        assert hub.get(GROUP)
        clock[0] = IDLE_CLOSE_MS
        await hub.reap()
        assert hub.get(GROUP) is None

    asyncio.run(run())
    reopened, _ = hub.open(GROUP, "3")
    assert [i.track for i in reopened.queue] == [make_track(1)], "queue survives close"
    assert reopened.owner == "3"


# ---------------------------------------------------------------- HTTP + WebSocket


class FakeSource:
    platform = "qq"

    def __init__(self) -> None:
        self.tracks = [make_track(1), make_track(2), make_track(3)]

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        return self.tracks[:limit]

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        return Stream(url=f"https://cdn.example/{track.id}.mp3", expires_in=7200, bitrate=bitrate)


@pytest.fixture
def env():
    bridge = FakeBridge()
    bridge.add("1", "房主")
    bridge.add("2", "群友")
    allowed = [GROUP]
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: allowed),
        bridge=bridge,
        store=Store(":memory:"),
        sources={"qq": FakeSource()},
    )
    client = TestClient(create_app(service), base_url=ORIGIN)
    return service, bridge, client, allowed


H = {"Origin": ORIGIN}


def login(client: TestClient, service: ListenService, qq: str) -> None:
    token = service.login_url(qq, GROUP).split("#t=", 1)[1]
    r = client.post("/api/login/link", json={"token": token}, headers=H)
    assert r.status_code == 200, r.text


def ws_headers(client: TestClient, origin: str = ORIGIN) -> dict[str, str]:
    # 会话 cookie 是 Secure 的，TestClient 的 WebSocket 走 ws://，不会自动带上，这里手动加。
    return {"Origin": origin, "Cookie": f"listen_session={client.cookies.get('listen_session')}"}


def test_http_login_and_security(env):
    service, bridge, client, allowed = env
    assert client.get("/api/me").status_code == 401
    page = client.get("/")
    assert page.status_code == 200 and "Content-Security-Policy" in page.headers
    assert "unsafe-inline" not in page.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]

    token = service.login_url("2", GROUP).split("#t=", 1)[1]
    assert client.post("/api/login/link", json={"token": token}, headers={"Origin": "https://evil.example"}
                       ).status_code == 403
    assert client.post("/api/login/link", json={"token": token}, headers=H).status_code == 200
    cookie = client.cookies.get("listen_session")
    assert cookie
    me = client.get("/api/me").json()
    assert me["qq"] == "2" and me["name"] == "群友" and not me["superuser"]
    assert client.post("/api/login/link", json={"token": token}, headers=H).status_code == 400, "single use"

    bridge.offline = True
    service.members.forget(GROUP)
    assert client.get("/api/me").status_code == 503
    bridge.offline = False

    allowed.clear()
    assert client.get("/api/me").status_code == 403, "group removed from whitelist"


def test_http_non_member_cannot_login(env):
    service, _, client, _ = env
    token = service.login_url("404", GROUP).split("#t=", 1)[1]
    assert client.post("/api/login/link", json={"token": token}, headers=H).status_code == 400


def test_http_code_login(env):
    service, _, client, _ = env
    r = client.post("/api/login/code", json={}, headers=H).json()
    code = r["code"]
    assert not r["done"]
    assert client.post("/api/login/code", json={}, headers=H).json() == {"ok": True, "done": False}
    assert service.redeem_code(code, "2", GROUP)
    r = client.post("/api/login/code", json={}, headers=H).json()
    assert r["done"] and r["name"] == "群友"
    assert client.get("/api/me").json()["qq"] == "2"


def test_http_search_and_stream(env):
    service, _, client, _ = env
    login(client, service, "2")
    tracks = client.get("/api/search", params={"q": "x"}).json()["tracks"]
    assert len(tracks) == 3
    ref = tracks[0]["ref"]
    assert client.get("/api/stream", params={"ref": ref}).status_code == 404, "not in a room queue"
    room, _ = service.hub.open(GROUP, "1")
    room.add(service.known_track(ref), "2", "群友")
    r = client.get("/api/stream", params={"ref": ref}).json()
    assert r["url"].endswith(".mp3") and r["bitrate"] == 320


def test_unplayable_current_track_is_skipped(env):
    service, _, client, _ = env
    login(client, service, "2")
    tracks = client.get("/api/search", params={"q": "x"}).json()["tracks"]
    room, _ = service.hub.open(GROUP, "1")
    for t in tracks[:2]:
        room.add(service.known_track(t["ref"]), "2", "群友")
    room.play("1", service.clock())
    broken = tracks[0]["ref"]

    async def fail(track: Track, bitrate: int = 320) -> Stream:
        raise SourceError("下架了")

    service.sources["qq"].stream = fail  # type: ignore[method-assign]
    r = client.get("/api/stream", params={"ref": broken})
    assert r.status_code == 502
    assert room.current is not None and room.current.track.ref == tracks[1]["ref"]
    assert broken not in [t.ref for t in room.history], "skipped songs aren't counted as played"


def test_websocket_flow(env):
    service, bridge, client, _ = env
    login(client, service, "2")
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws", headers=ws_headers(client, "https://evil.example")) as ws:
            ws.receive_json()
    assert denied.value.code == 4403
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws", headers=H) as ws:
            ws.receive_json()
    assert denied.value.code == 4401, "no cookie"

    with client.websocket_connect("/ws", headers=ws_headers(client)) as ws:
        assert ws.receive_json()["type"] == "closed", "no room yet"

    service.hub.open(GROUP, "1")
    ref = client.get("/api/search", params={"q": "x"}).json()["tracks"][0]["ref"]

    def until(ws, kind):
        for _ in range(20):
            msg = ws.receive_json()
            assert msg.get("message") != "操作太快了，慢一点", "test hit the per-second rate limit"
            if msg["type"] == kind:
                return msg
        raise AssertionError(kind)

    def send(ws, msg):
        time.sleep(0.21)  # 服务端每个连接每秒最多 5 条
        ws.send_json(msg)

    with client.websocket_connect("/ws", headers=ws_headers(client)) as ws:
        assert ws.receive_json()["type"] == "hello"
        state = until(ws, "state")["state"]
        assert state["current"] is None
        until(ws, "members")

        send(ws, {"type": "ping", "client_ms": 5})
        assert until(ws, "pong")["client_ms"] == 5

        send(ws, {"type": "queue", "op": "add", "ref": ref})
        state = until(ws, "state")["state"]
        assert state["current"]["track"]["ref"] == ref and not state["paused"], "first song starts playing"

        send(ws, {"type": "control", "action": "pause", "version": state["version"]})
        state = until(ws, "state")["state"]
        assert state["paused"]
        assert "暂停" in until(ws, "chat")["text"]

        send(ws, {"type": "control", "action": "play", "version": state["version"] - 1})
        assert until(ws, "error")["message"]

        send(ws, {"type": "queue", "op": "add", "ref": "qq:notsearched"})
        assert "找不到" in until(ws, "error")["message"]

        vip = Track(platform="qq", id="00vip0000001", title="会员歌", artists="x", vip=True)
        service.remember([vip])
        send(ws, {"type": "queue", "op": "add", "ref": vip.ref})
        assert "会员" in until(ws, "error")["message"]

        send(ws, {"type": "chat", "text": "你好"})
        chat = until(ws, "chat")
        assert chat["text"] == "你好" and chat["name"] == "群友"

        send(ws, {"type": "setting", "allow_control": False})
        assert "房主" in until(ws, "error")["message"]

        asyncio.run(service.member_left(GROUP, "2"))
        assert until(ws, "closed")["message"]
    assert client.get("/api/me").status_code == 401, "session revoked after leaving the group"
