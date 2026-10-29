"""点歌的音源：网易云、酷狗（用假的 HTTP 响应）、熔断、判断是不是同一首歌。

运行：python -m pytest tests/test_music_sources.py -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from mytest.plugins.music_plugin.sources.base import (
    SourceError,
    SourceUnavailable,
    Stream,
    Track,
    https,
)
from mytest.plugins.music_plugin.sources.kugou import KugouSource
from mytest.plugins.music_plugin.sources.netease import NeteaseSource
from mytest.plugins.music_plugin.sources.resolver import (
    COOLDOWN,
    FAIL_LIMIT,
    Sources,
    same_song,
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


def test_three_platform_live_search():
    """三个平台真的都能搜到歌，默认跳过。本地起好 api-enhanced 和 KuGouMusicApi 后：
    MUSIC_LIVE_NCM=http://127.0.0.1:13000 MUSIC_LIVE_KG=http://127.0.0.1:13001 pytest -k three_platform
    """
    import os

    from mytest.plugins.music_plugin.service import MusicService
    from mytest.plugins.music_plugin.sources.qqmusic import QQMusicSource

    ncm_url, kg_url = os.environ.get("MUSIC_LIVE_NCM", ""), os.environ.get("MUSIC_LIVE_KG", "")
    if not ncm_url or not kg_url:
        pytest.skip("没有设置 MUSIC_LIVE_NCM / MUSIC_LIVE_KG")

    async def go() -> dict[str, list[str]]:
        sources = {"qq": QQMusicSource(), "netease": NeteaseSource(ncm_url, timeout=15),
                   "kugou": KugouSource(kg_url, timeout=15)}
        service = MusicService(sources)
        out = {}
        for keyword in ("晴天 周杰伦", "Lemon 米津玄師", "春日影 MyGO"):
            results = await service.search_all(keyword, 3)
            out[keyword] = [f"{r.platform}:{len(r.tracks)}:{r.error}" for r in results]
        for src in sources.values():
            await src.close()
        return out

    result = run(go())
    print(json.dumps(result, ensure_ascii=False))
    for rows in result.values():
        counts = {row.split(":")[0]: int(row.split(":")[1]) for row in rows}
        assert set(counts) == {"qq", "netease", "kugou"} and all(counts.values()), rows


def test_live_services_contract():
    """真实接口的冒烟测试，默认跳过。本地起好 api-enhanced 和 KuGouMusicApi 后：
    MUSIC_LIVE_NCM=http://127.0.0.1:13000 MUSIC_LIVE_KG=http://127.0.0.1:13001 pytest -k live
    """
    import os

    ncm_url, kg_url = os.environ.get("MUSIC_LIVE_NCM", ""), os.environ.get("MUSIC_LIVE_KG", "")
    if not ncm_url or not kg_url:
        pytest.skip("没有设置 MUSIC_LIVE_NCM / MUSIC_LIVE_KG")

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
