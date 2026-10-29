"""一起听 M2：网易云、酷狗音源（用假的 HTTP 响应）、熔断、跨平台换源、歌词、资料卡。

运行：python -m pytest tests/test_listen_sources.py -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mytest.plugins.listen_plugin.app import create_app
from mytest.plugins.listen_plugin.service import ListenConfig, ListenService
from mytest.plugins.listen_plugin.sources.base import (
    SourceError,
    SourceUnavailable,
    Stream,
    Track,
    https,
)
from mytest.plugins.listen_plugin.sources.kugou import KugouSource
from mytest.plugins.listen_plugin.sources.netease import NeteaseSource
from mytest.plugins.listen_plugin.sources.resolver import (
    COOLDOWN,
    FAIL_LIMIT,
    Sources,
    same_song,
)
from mytest.plugins.listen_plugin.store import Store

from .test_listen import (
    GROUP,
    ORIGIN,
    SECRET,
    FakeBridge,
    FakeSource,
    login,
    ws_headers,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def mock(routes: dict[str, Any], calls: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        body = routes.get(request.url.path)
        if callable(body):
            body = body(request)
        if body is None:
            return httpx.Response(404, json={"code": 404})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- 工具函数


def test_https_upgrade():
    assert https("http://p1.music.126.net/a.jpg") == "https://p1.music.126.net/a.jpg"
    assert https("https://x/y") == "https://x/y"
    for bad in (None, "", "javascript:alert(1)", "ftp://x", "//x"):
        assert https(bad) == ""


def test_same_song():
    a = Track("qq", "a", "晴天", "周杰伦", duration_ms=269_000)
    assert same_song(a, Track("netease", "1", "晴天", "周杰伦", duration_ms=269_500))
    assert same_song(a, Track("kugou", "B" * 32, "晴天", "周杰伦、林俊杰", duration_ms=270_000)), "artists overlap"
    assert same_song(a, Track("netease", "2", "晴 天", "周杰伦", duration_ms=0)), "unknown duration is ok"
    assert not same_song(a, Track("netease", "3", "晴天 (Live)", "周杰伦", duration_ms=269_000))
    assert not same_song(a, Track("netease", "4", "晴天", "Lucky小爱", duration_ms=269_000))
    assert not same_song(a, Track("netease", "5", "晴天", "周杰伦", duration_ms=280_000))
    assert same_song(Track("qq", "x", "ステキハピネス", "中村繪里子"), Track("netease", "y", "ｽﾃｷﾊﾋﾟﾈｽ", "中村繪里子")), \
        "NFKC folds half-width katakana"
    mygo = Track("qq", "a", "春日影 (MyGO!!!!! ver.)", "MyGO!!!!!", duration_ms=256_000)
    assert same_song(mygo, Track("kugou", "b", "春日影", "MyGO!!!!!", duration_ms=256_000)), "band-name ver. suffix"
    assert not same_song(
        Track("qq", "a", "春日影 (CRYCHIC ver.)", "MyGO!!!!!", duration_ms=256_000),
        Track("kugou", "b", "春日影", "MyGO!!!!!", duration_ms=256_000),
    ), "a different band's version is a different song"


# ---------------------------------------------------------------- 网易云


NCM_SEARCH = {
    "code": 200,
    "result": {"songs": [
        {"id": 186016, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美", "picUrl": "http://p1.music.126.net/a.jpg"},
         "dt": 269_000, "fee": 1},
        {"id": 31514994, "name": "ステキハピネス", "ar": [{"name": "中村繪里子"}], "al": {"name": "x", "picUrl": ""},
         "dt": 234_080, "fee": 0},
        {"id": "bad", "name": "x"},
    ]},
}


def test_netease_search_and_stream():
    calls: list[httpx.Request] = []
    routes = {
        "/cloudsearch": NCM_SEARCH,
        "/song/url/v1": lambda r: {"code": 200, "data": [
            {"id": 31514994, "url": "http://m801.music.126.net/x.mp3", "br": 320002, "expi": 1200, "freeTrialInfo": None}
            if r.url.params["id"] == "31514994" else
            {"id": 186016, "url": "http://m801.music.126.net/trial.mp3", "br": 128000, "freeTrialInfo": {"start": 0, "end": 30}}
        ]},
        "/lyric": {"code": 200, "lrc": {"lyric": "[00:01.00]a"}, "tlyric": {"lyric": "[00:01.00]b"}},
    }
    src = NeteaseSource("http://ncm", transport=mock(routes, calls))
    tracks = run(src.search("晴天"))
    assert [t.id for t in tracks] == ["186016", "31514994"]
    assert tracks[0].vip and not tracks[1].vip
    assert tracks[0].cover == "https://p1.music.126.net/a.jpg?param=300y300"
    assert tracks[1].cover == ""
    stream = run(src.stream(tracks[1], 320))
    assert stream.url == "https://m801.music.126.net/x.mp3" and stream.bitrate == 320 and stream.expires_in == 1200
    assert calls[-1].url.params["level"] == "exhigh"
    with pytest.raises(SourceError, match="会员"):  # 30 秒试听当作拿不到
        run(src.stream(tracks[0], 320))
    assert run(src.lyrics(tracks[1])) == ("[00:01.00]a", "[00:01.00]b")


def test_netease_errors_are_classified():
    src = NeteaseSource("http://ncm", transport=mock({"/cloudsearch": httpx.Response(502, text="<html>")}))
    with pytest.raises(SourceUnavailable):
        run(src.search("x"))
    src = NeteaseSource("http://ncm", transport=mock({"/cloudsearch": {"code": 400, "message": "参数错误"}}))
    with pytest.raises(SourceError, match="参数错误") as e:
        run(src.search("x"))
    assert not isinstance(e.value, SourceUnavailable)


# ---------------------------------------------------------------- 酷狗


KG_SEARCH = {
    "status": 1,
    "data": {"lists": [
        {"FileHash": "14ad8c9e9c475a9212dabedb8c65edf5", "SongName": "ステキハピネス", "Singers": [{"name": "中村繪里子"}],
         "AlbumName": "x", "AlbumID": "953125", "MixSongID": "31975470", "Duration": 234, "Privilege": 0,
         "Image": "http://imge.kugou.com/stdmusic/{size}/a.jpg"},
        {"FileHash": "B3A52A7A958BF0AED0EBFBA2E9A818B7", "SongName": "晴天", "SingerName": "周杰伦", "AlbumID": "966846",
         "MixSongID": "32100650", "Duration": 269, "Privilege": 10},
        {"FileHash": "../../etc", "SongName": "bad"},
    ]},
}


def test_kugou_search_and_stream_registers_device():
    calls: list[httpx.Request] = []
    state = {"registered": 0, "verified": False}

    def register(_: httpx.Request) -> dict:
        state["registered"] += 1
        return {"status": 1, "data": {"dfid": f"dev{state['registered']}"}}

    def song_url(r: httpx.Request) -> dict:
        # 第一个 dfid 被判定为需要验证，换一个后才给地址
        if r.url.params["cookie"] == "dfid=dev1":
            return {"status": 2, "errcode": 20028, "error": "本次请求需要验证"}
        if r.url.params["hash"].startswith("B3A5"):
            return {"status": 2, "url": []}
        return {"status": 1, "url": ["http://fsandroid.tx.kugou.com/x.mp3"], "backupUrl": []}

    routes = {"/search": KG_SEARCH, "/register/dev": register, "/song/url": song_url}
    src = KugouSource("http://kg", transport=mock(routes, calls))
    tracks = run(src.search("x"))
    assert [t.id for t in tracks] == ["14AD8C9E9C475A9212DABEDB8C65EDF5", "B3A52A7A958BF0AED0EBFBA2E9A818B7"]
    assert tracks[0].file_id == "953125_31975470" and not tracks[0].vip
    assert tracks[0].cover == "https://imge.kugou.com/stdmusic/400/a.jpg"
    assert tracks[1].vip and tracks[1].artists == "周杰伦"
    stream = run(src.stream(tracks[0], 320))
    assert stream.url == "https://fsandroid.tx.kugou.com/x.mp3"
    assert state["registered"] == 2, "re-registers once when dfid is rejected"
    url_calls = [c for c in calls if c.url.path == "/song/url"]
    assert url_calls[-1].url.params["album_audio_id"] == "31975470"
    run(src.stream(tracks[0], 128))
    assert state["registered"] == 2, "dfid is cached"
    with pytest.raises(SourceError, match="会员"):
        run(src.stream(tracks[1], 320))


def test_kugou_lyrics():
    routes = {
        "/search/lyric": {"candidates": [{"id": "50334405", "accesskey": "ABC"}]},
        "/lyric": lambda r: {"decodeContent": "[00:00.00]x"} if r.url.params["accesskey"] == "ABC" else {},
    }
    src = KugouSource("http://kg", transport=mock(routes))
    assert run(src.lyrics(Track("kugou", "A" * 32, "x", "y"))) == ("[00:00.00]x", "")
    src = KugouSource("http://kg", transport=mock({"/search/lyric": {"candidates": []}}))
    assert run(src.lyrics(Track("kugou", "A" * 32, "x", "y"))) == ("", "")


# ---------------------------------------------------------------- 熔断


class Flaky:
    platform = "netease"
    vip_ready = False

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "down"

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        self.calls += 1
        if self.mode == "down":
            raise SourceUnavailable("网易云接口连不上")
        if self.mode == "empty":
            raise SourceError("没有这首")
        return []

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        raise SourceError("x")

    async def lyrics(self, track: Track) -> tuple[str, str]:
        return "", ""


def test_circuit_breaker():
    clock = [0.0]
    flaky = Flaky()
    sources = Sources({"netease": flaky}, clock=lambda: clock[0])
    for _ in range(FAIL_LIMIT):
        with pytest.raises(SourceUnavailable):
            run(sources.search("netease", "x"))
    assert not sources.available("netease")
    with pytest.raises(SourceError, match="暂时不可用"):
        run(sources.search("netease", "x"))
    assert flaky.calls == FAIL_LIMIT, "open breaker doesn't call the source"
    clock[0] = COOLDOWN
    flaky.mode = "ok"
    assert run(sources.search("netease", "x")) == []
    flaky.mode = "empty"
    for _ in range(FAIL_LIMIT * 2):
        with pytest.raises(SourceError):
            run(sources.search("netease", "x"))
    assert sources.available("netease"), "business errors don't trip the breaker"


def test_slow_source_times_out():
    class Slow(Flaky):
        async def search(self, keyword: str, limit: int = 20) -> list[Track]:
            await asyncio.sleep(1)
            return []

    sources = Sources({"netease": Slow()})

    async def go():
        return await sources.call("netease", "search", "x", 20, timeout=0.05)

    with pytest.raises(SourceError, match="超时"):
        run(go())


# ---------------------------------------------------------------- 跨平台


VIP_QQ = Track("qq", "0039MnYb0qxYhV", "晴天", "周杰伦", duration_ms=269_000, vip=True)
FREE_NCM = Track("netease", "186016", "晴天", "周杰伦", duration_ms=269_500)
LIVE_NCM = Track("netease", "186017", "晴天 (Live)", "周杰伦", duration_ms=269_000)
VIP_KG = Track("kugou", "B" * 32, "晴天", "周杰伦", duration_ms=269_000, vip=True)


@pytest.fixture
def multi():
    bridge = FakeBridge()
    bridge.add("1", "房主")
    bridge.add("2", "群友")
    qq = FakeSource("qq", [VIP_QQ])
    ncm = FakeSource("netease", [LIVE_NCM, FREE_NCM])
    kg = FakeSource("kugou", [VIP_KG])
    service = ListenService(
        config=ListenConfig(public_url=ORIGIN, secret=SECRET, allowed_groups=lambda: [GROUP]),
        bridge=bridge,
        store=Store(":memory:"),
        source_map={"qq": qq, "netease": ncm, "kugou": kg},
    )
    client = TestClient(create_app(service), base_url=ORIGIN)
    return service, client, qq, ncm, kg


def test_substitute_finds_playable_copy(multi):
    service, _, _, ncm, _ = multi
    assert run(service.substitute(VIP_QQ)) == FREE_NCM, "skips the Live version and the VIP kugou copy"
    ncm.tracks = [LIVE_NCM]
    assert run(service.substitute(VIP_QQ)) is None


def test_platform_list_and_search(multi):
    service, client, *_ = multi
    login(client, service, "2")
    platforms = client.get("/api/platforms").json()["platforms"]
    assert [p["id"] for p in platforms] == ["qq", "netease", "kugou"]
    assert all(p["available"] for p in platforms)
    r = client.get("/api/search", params={"q": "晴天", "platform": "netease"}).json()
    assert [t["ref"] for t in r["tracks"]] == [LIVE_NCM.ref, FREE_NCM.ref]
    assert client.get("/api/search", params={"q": "x", "platform": "spotify"}).status_code == 400


def until(ws: Any, kind: str) -> dict:
    for _ in range(20):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(kind)


def test_adding_vip_song_uses_other_platform(multi):
    service, client, *_ = multi
    login(client, service, "2")
    service.hub.open(GROUP, "1")
    ref = client.get("/api/search", params={"q": "晴天", "platform": "qq"}).json()["tracks"][0]["ref"]
    with client.websocket_connect("/ws", headers=ws_headers(client)) as ws:
        until(ws, "members")
        ws.send_json({"type": "queue", "op": "add", "ref": ref})
        chat = until(ws, "chat")
        assert "换成网易云音乐" in chat["text"]
        state = until(ws, "state")["state"]
        assert state["current"]["track"]["ref"] == FREE_NCM.ref


def test_playback_failure_switches_platform(multi):
    service, client, qq, ncm, _ = multi
    login(client, service, "2")
    free_qq = Track("qq", "0001OKOk2tCZb8", "晴天", "周杰伦", duration_ms=269_000)
    service.remember([free_qq])
    room, _ = service.hub.open(GROUP, "1")
    room.add(free_qq, "2", "群友")
    room.play("2", service.clock())
    qq.broken.add(free_qq.id)

    first = client.get("/api/stream", params={"ref": free_qq.ref})
    assert first.status_code == 502
    assert room.current is not None and room.current.track == FREE_NCM
    # 另一个听众晚一步报同一首的错：旧的已经不在队列里了，不会再换一次
    late = client.get("/api/stream", params={"ref": free_qq.ref})
    assert late.status_code == 404
    assert room.current.track == FREE_NCM
    assert room.current.added_by == "2", "keeps who requested it"
    r = client.get("/api/stream", params={"ref": FREE_NCM.ref}).json()
    assert r["url"] == f"https://cdn.example/netease/{FREE_NCM.id}.mp3"


# ---------------------------------------------------------------- 歌词、资料卡


def test_concurrent_failures_switch_once(multi):
    """多个听众同时报同一首放不了：只换一次源。直接调用服务端的处理函数，不经过 TestClient（它不适合并发）。"""
    service, _, qq, ncm, _ = multi
    free_qq = Track("qq", "0001OKOk2tCZb8", "晴天", "周杰伦", duration_ms=269_000)
    room, _ = service.hub.open(GROUP, "1")
    room.add(free_qq, "2", "群友")
    room.play("2", service.clock())
    searches = []
    original = ncm.search

    async def slow_search(keyword: str, limit: int = 20) -> list[Track]:
        searches.append(keyword)
        await asyncio.sleep(0.05)
        return await original(keyword, limit)

    ncm.search = slow_search  # type: ignore[method-assign]
    app = create_app(service)
    handler = app.state.handle_unplayable

    async def go():
        await asyncio.gather(*(handler(GROUP, free_qq, "下架了") for _ in range(3)))

    run(go())
    assert room.current is not None and room.current.track == FREE_NCM
    assert len(searches) == 1, "the lock lets only one listener trigger the fallback"
    assert [t for t in room.history] == [], "replacement is not counted as a separate play"


def test_lyrics_endpoint_and_cache(multi):
    service, client, _, ncm, _ = multi
    login(client, service, "2")
    client.get("/api/search", params={"q": "晴天", "platform": "netease"})
    calls = []
    original = ncm.lyrics

    async def counting(track: Track) -> tuple[str, str]:
        calls.append(track.ref)
        return await original(track)

    ncm.lyrics = counting  # type: ignore[method-assign]
    r = client.get("/api/lyrics", params={"ref": FREE_NCM.ref}).json()
    assert r["lrc"].startswith("[00:01.00]晴天") and r["trans"] == "[00:01.00]translated"
    client.get("/api/lyrics", params={"ref": FREE_NCM.ref})
    assert calls == [FREE_NCM.ref], "cached"
    assert client.get("/api/lyrics", params={"ref": "qq:neverseen00"}).status_code == 404


def test_profile_only_for_group_members(multi):
    service, client, *_ = multi
    login(client, service, "2")
    room, _ = service.hub.open(GROUP, "1")
    service.remember([FREE_NCM])
    room.add(FREE_NCM, "1", "房主")
    p = client.get("/api/members/1").json()
    assert p["name"] == "房主" and p["signature"] == "1 的签名" and p["qq_level"] == 12
    assert p["in_queue"] == 1 and not p["playing_theirs"] and not p["online"]
    room.play("1", service.clock())
    p = client.get("/api/members/1").json()
    assert p["in_queue"] == 0 and p["playing_theirs"], "the playing song isn't counted as queued"
    assert client.get("/api/members/404").status_code == 404, "not in the group"
    assert client.get("/api/members/abc").status_code == 404
    client.cookies.clear()
    assert client.get("/api/members/1").status_code == 401


def test_live_services_contract():
    """真实接口的冒烟测试，默认跳过。本地起好 api-enhanced 和 KuGouMusicApi 后：
    LISTEN_LIVE_NCM=http://127.0.0.1:13000 LISTEN_LIVE_KG=http://127.0.0.1:13001 pytest -k live
    """
    import os

    ncm_url, kg_url = os.environ.get("LISTEN_LIVE_NCM", ""), os.environ.get("LISTEN_LIVE_KG", "")
    if not ncm_url or not kg_url:
        pytest.skip("没有设置 LISTEN_LIVE_NCM / LISTEN_LIVE_KG")

    async def go() -> dict:
        ncm, kg = NeteaseSource(ncm_url, timeout=15), KugouSource(kg_url, timeout=15)
        out: dict[str, Any] = {}
        for name, src in (("ncm", ncm), ("kg", kg)):
            tracks = await src.search("ステキハピネス M@STER", 5)
            free = next(t for t in tracks if not t.vip)
            stream = await src.stream(free, 320)
            async with httpx.AsyncClient(timeout=15) as c:
                head = await c.get(stream.url, headers={"Range": "bytes=0-1023"})
            lrc, _ = await src.lyrics(free)
            out[name] = {"title": free.title, "status": head.status_code, "lrc": bool(lrc), "https": stream.url[:8]}
            await src.close()
        return out

    result = run(go())
    print(json.dumps(result, ensure_ascii=False))
    for item in result.values():
        assert item["status"] in (200, 206) and item["lrc"] and item["https"] == "https://"
