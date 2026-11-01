"""B 站：签名、链接识别、搜索和看视频。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from mytest.plugins._shared.services import bilibili as bili
from mytest.plugins._shared.services.http import ServiceError

IMG = "7cd084941338484aae1ad9425b84077c"
SUB = "4932caff0ff746eab6f01bf08b70ac45"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(bili, "_keys", None)
    monkeypatch.setattr(bili, "_buvid", {})


# ---------------------------------------------------------------- 纯函数


def test_mixin_key_matches_documented_example():
    # bilibili-API-collect 文档里的例子
    assert bili.mixin_key(IMG, SUB) == "ea1db124af3c7062474693fa704f4ff8"


def test_sign_matches_documented_example():
    signed = bili.sign({"foo": "114", "bar": "514", "zab": 1919810}, IMG, SUB, now=1702204169)
    assert signed["wts"] == "1702204169"
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def test_wbi_query_encoding():
    assert bili.wbi_query({"foo": "one one four", "bar": "五一四", "baz": 1919810}) == \
        "bar=%E4%BA%94%E4%B8%80%E5%9B%9B&baz=1919810&foo=one%20one%20four"
    assert bili.wbi_query({"k": "a!b'c(d)e"}) == "k=abcde"


@pytest.mark.parametrize("text, ref", [
    ("https://www.bilibili.com/video/BV1Hk4y1B7Cx/?spm=x", bili.VideoRef(bvid="BV1Hk4y1B7Cx")),
    ("看看这个 BV1Hk4y1B7Cx", bili.VideoRef(bvid="BV1Hk4y1B7Cx")),
    ("https://www.bilibili.com/video/BV1Hk4y1B7Cx?p=3", bili.VideoRef(bvid="BV1Hk4y1B7Cx", page=3)),
    ("https://m.bilibili.com/video/av170001", bili.VideoRef(aid=170001)),
    ("av170001", bili.VideoRef(aid=170001)),
])
def test_parse_ref(text, ref):
    assert bili.parse_ref(text) == ref


@pytest.mark.parametrize("text", ["", "你好", "BV123", "favorite", "https://b23.tv/abcd123", "nav1"])
def test_parse_ref_rejects(text):
    assert bili.parse_ref(text) is None


def test_short_link_and_media_url():
    assert bili.find_short_link("【标题】 https://b23.tv/AbC1234 复制") == "https://b23.tv/AbC1234"
    assert bili.find_short_link("https://evil.com/b23.tv/x") == ""
    assert bili.media_url("//i0.hdslb.com/bfs/a.jpg") == "https://i0.hdslb.com/bfs/a.jpg"
    assert bili.media_url("http://i0.hdslb.com/a.jpg") == "https://i0.hdslb.com/a.jpg"
    assert bili.media_url("https://evil.com/a.jpg") == ""
    assert bili.media_url("https://hdslb.com.evil.com/a.jpg") == ""
    assert bili.media_url("https://127.0.0.1/a.jpg") == ""


def test_cookie_parsing():
    assert bili.parse_cookie("") == {}
    assert bili.parse_cookie(" abc123%2C ") == {"SESSDATA": "abc123%2C"}
    assert bili.parse_cookie("SESSDATA=x; bili_jct=y; buvid3=z") == {"SESSDATA": "x", "bili_jct": "y", "buvid3": "z"}


def test_text_helpers():
    assert bili.clean_text('【<em class="keyword">洛天依</em>】生日会 &amp; 直播') == "【洛天依】生日会 & 直播"
    assert bili.count_text(269428) == "26.9万" and bili.count_text(10000) == "1万" and bili.count_text(999) == "999"
    assert bili.duration_text(65) == "1:05" and bili.duration_text(3725) == "1:02:05"
    assert bili.duration_text("4:18") == "4:18"


def test_pick_subtitle_prefers_chinese():
    subs = [
        {"lan": "en-US", "subtitle_url": "//aisubtitle.hdslb.com/en.json"},
        {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/ai.json"},
        {"lan": "zh-Hans", "subtitle_url": "//aisubtitle.hdslb.com/zh.json"},
        {"lan": "zh-CN", "subtitle_url": "https://evil.com/zh.json"},
    ]
    assert bili.pick_subtitle(subs) == "https://aisubtitle.hdslb.com/zh.json"
    assert bili.pick_subtitle(subs[:2]) == "https://aisubtitle.hdslb.com/ai.json"
    assert bili.pick_subtitle([]) == ""


def test_subtitle_text_truncates():
    data = {"body": [{"content": "大家好"}, {"content": " "}, {"content": "今天做红烧肉"}]}
    assert bili.subtitle_text(data) == "大家好，今天做红烧肉"
    assert bili.subtitle_text({"body": [{"content": "字" * 50}]}, limit=10).endswith("……（后面还有）")


# ---------------------------------------------------------------- 接口（假的 B 站）


SEARCH_ITEM = {
    "type": "video", "bvid": "BV1Hk4y1B7Cx", "aid": 753839250, "author": "洛天依",
    "title": '【<em class="keyword">洛天依</em>】生日会', "description": "八年", "duration": "58:6",
    "play": 269428, "pic": "//i2.hdslb.com/bfs/archive/x.jpg",
}
VIEW = {
    "bvid": "BV1Hk4y1B7Cx", "aid": 753839250, "cid": 111, "title": "生日会", "desc": "八年回忆",
    "duration": 3486, "owner": {"name": "洛天依"}, "stat": {"view": 269428},
    "pic": "http://i2.hdslb.com/bfs/archive/x.jpg",
    "pages": [{"cid": 111, "duration": 3486}, {"cid": 222, "duration": 60}],
}


def fake_bilibili(*, search_code=0, logged_in=False, subtitles=None, short_target="https://www.bilibili.com/video/BV1Hk4y1B7Cx"):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path, host = request.url.path, request.url.host
        if host == "b23.tv":
            return httpx.Response(302, headers={"location": short_target})
        if host.endswith("hdslb.com"):
            if path.endswith(".json"):
                return httpx.Response(200, json={"body": [{"content": "大家好"}, {"content": "今天是生日会"}]})
            return httpx.Response(200, content=b"JPEG")
        if path == "/x/frontend/finger/spi":
            return httpx.Response(200, json={"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})
        if path == "/x/web-interface/nav":
            data = {"wbi_img": {"img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG}.png",
                                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB}.png"},
                    "isLogin": logged_in, "uname": "小号" if logged_in else ""}
            return httpx.Response(200, json={"code": 0 if logged_in else -101, "data": data})
        if path == "/x/web-interface/wbi/search/type":
            if search_code:
                return httpx.Response(200, json={"code": search_code, "message": "拦截"})
            return httpx.Response(200, json={"code": 0, "data": {"result": [SEARCH_ITEM, {"bvid": "bad"}]}})
        if path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": VIEW})
        if path == "/x/player/wbi/v2":
            subs = subtitles if subtitles is not None else []
            return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": subs}}})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def test_search_signs_and_sends_buvid():
    transport, calls = fake_bilibili()
    api = bili.Bilibili(transport=transport)
    videos = asyncio.run(api.search("洛天依", order="click"))
    assert [v.bvid for v in videos] == ["BV1Hk4y1B7Cx"], "invalid items are skipped"
    v = videos[0]
    assert (v.title, v.author, v.play, v.cover) == ("【洛天依】生日会", "洛天依", "26.9万", "https://i2.hdslb.com/bfs/archive/x.jpg")
    search = next(c for c in calls if c.url.path.endswith("/search/type"))
    params = dict(search.url.params)
    assert params["keyword"] == "洛天依" and params["order"] == "click" and params["w_rid"] and params["wts"]
    assert "buvid3=B3" in search.headers.get("cookie", "")
    assert search.headers["referer"] == "https://www.bilibili.com/"

    asyncio.run(api.search("猫"))
    assert sum(c.url.path == "/x/web-interface/nav" for c in calls) == 1, "wbi keys cached"
    assert sum(c.url.path == "/x/frontend/finger/spi" for c in calls) == 1, "buvid cached"


def test_search_blocked_is_explained():
    transport, _ = fake_bilibili(search_code=-412)
    with pytest.raises(ServiceError, match="拦截"):
        asyncio.run(bili.Bilibili(transport=transport).search("猫"))


def test_resolve_short_link():
    transport, calls = fake_bilibili()
    api = bili.Bilibili(transport=transport)
    assert asyncio.run(api.resolve("快看 https://b23.tv/AbC1234")) == bili.VideoRef(bvid="BV1Hk4y1B7Cx")
    assert asyncio.run(api.resolve("BV1Hk4y1B7Cx")).bvid == "BV1Hk4y1B7Cx" and len(calls) == 1, "no request needed"
    with pytest.raises(ServiceError, match="没认出"):
        asyncio.run(api.resolve("你好"))


def test_resolve_rejects_redirect_off_bilibili():
    transport, _ = fake_bilibili(short_target="https://evil.com/video/BV1Hk4y1B7Cx")
    with pytest.raises(ServiceError, match="没有跳到"):
        asyncio.run(bili.Bilibili(transport=transport).resolve("https://b23.tv/AbC1234"))
    transport, _ = fake_bilibili(short_target="https://live.bilibili.com/123")
    with pytest.raises(ServiceError, match="不是视频"):
        asyncio.run(bili.Bilibili(transport=transport).resolve("https://b23.tv/AbC1234"))


def test_video_and_subtitle():
    subs = [{"lan": "zh-Hans", "subtitle_url": "//aisubtitle.hdslb.com/bfs/zh.json"}]
    transport, calls = fake_bilibili(logged_in=True, subtitles=subs)
    api = bili.Bilibili(cookie="SESSDATA=secret", transport=transport)
    video = asyncio.run(api.video(bili.VideoRef(bvid="BV1Hk4y1B7Cx", page=2)))
    assert (video.cid, video.duration, video.author, video.play) == (222, "1:00", "洛天依", "26.9万")
    assert asyncio.run(api.subtitle(video)) == "大家好，今天是生日会"
    assert asyncio.run(api.cover(video)) == b"JPEG"
    assert asyncio.run(api.login_name()) == "小号"
    media = [c for c in calls if c.url.host.endswith("hdslb.com")]
    assert media and all("SESSDATA" not in c.headers.get("cookie", "") for c in media), "cookie only for bilibili.com"
    assert any("SESSDATA=secret" in c.headers.get("cookie", "") for c in calls if c.url.host == "api.bilibili.com")


def test_no_subtitles():
    transport, _ = fake_bilibili(subtitles=[])
    api = bili.Bilibili(transport=transport)
    video = asyncio.run(api.video(bili.VideoRef(bvid="BV1Hk4y1B7Cx")))
    assert asyncio.run(api.subtitle(video)) == ""
    assert asyncio.run(api.login_name()) == ""


# ---------------------------------------------------------------- 工具


@pytest.fixture
def sent(monkeypatch):
    import mytest.plugins.tools_api_plugin.tools as tools

    messages = []

    async def fake_send(ctx, message, *, quote=False):
        messages.append(str(message))

    monkeypatch.setattr(tools, "send", fake_send)
    return messages


@pytest.fixture
def fake_api(monkeypatch):
    from mytest.plugins.tools_api_plugin import bilibili as tool_bili

    subs = [{"lan": "zh-Hans", "subtitle_url": "//aisubtitle.hdslb.com/bfs/zh.json"}]
    transport, calls = fake_bilibili(logged_in=True, subtitles=subs)
    monkeypatch.setattr(tool_bili, "client", lambda: bili.Bilibili(cookie="SESSDATA=x", transport=transport))
    return calls


def run_spawned(monkeypatch):
    import mytest.plugins.tools_api_plugin.tools as tools

    jobs = []
    monkeypatch.setattr(tools, "_spawn", lambda name, coro: jobs.append(coro))
    return jobs


def test_bilibili_search_tool(sent, fake_api, monkeypatch):
    from mytest.plugins.tools_api_plugin.tools import TOOLS, ToolContext, ToolError

    jobs = run_spawned(monkeypatch)
    ctx = ToolContext("123456", "654321")
    result = asyncio.run(TOOLS["bilibili_search"].run(ctx, {"keyword": "洛天依", "order": "popular"}))
    assert "【洛天依】生日会" in result["content"] and "不要再报标题" in result["content"]
    assert sent == []
    for job in jobs:
        asyncio.run(job)
    assert len(sent) == 1 and "https://www.bilibili.com/video/BV1Hk4y1B7Cx" in sent[0] and "image" in sent[0]
    with pytest.raises(ToolError, match="关键词"):
        asyncio.run(TOOLS["bilibili_search"].run(ctx, {"keyword": " "}))
    with pytest.raises(ToolError, match="order"):
        asyncio.run(TOOLS["bilibili_search"].run(ctx, {"keyword": "猫", "order": "hot"}))


def test_bilibili_watch_tool_with_link(sent, fake_api):
    from mytest.plugins.tools_api_plugin.tools import TOOLS, ToolContext

    result = asyncio.run(TOOLS["bilibili_watch"].run(ToolContext("123456", "654321"),
                                                      {"link": "https://b23.tv/AbC1234"}))
    assert "视频「生日会」" in result["content"] and "大家好，今天是生日会" in result["content"]
    assert "八年回忆" in result["content"] and sent == [], "watching sends nothing"


def test_bilibili_watch_tool_reads_share_card(sent, fake_api, monkeypatch):
    """群友发的是分享卡片（json 段），凛音看不到链接：从被回复的那条消息里找。"""
    import mytest.plugins.tools_api_plugin.tools as tools

    card = json.dumps({"meta": {"detail_1": {"qqdocurl": "https://b23.tv/AbC1234"}}}).replace("/", "\\/")
    messages = {
        100: {"message": [{"type": "reply", "data": {"id": "99"}}, {"type": "text", "data": {"text": "这讲的啥"}}]},
        99: {"message": [{"type": "json", "data": {"data": card}}]},
    }

    class FakeBot:
        async def get_msg(self, message_id):
            return messages[message_id]

    async def fake_bot():
        return FakeBot()

    monkeypatch.setattr(tools, "_bot", fake_bot)
    ctx = tools.ToolContext("123456", "654321", reply_to="100")
    result = asyncio.run(tools.TOOLS["bilibili_watch"].run(ctx, {}))
    assert "视频「生日会」" in result["content"]


def test_bilibili_watch_tool_without_link(sent, fake_api):
    from mytest.plugins.tools_api_plugin.tools import TOOLS, ToolContext, ToolError

    with pytest.raises(ToolError, match="抖音"):
        asyncio.run(TOOLS["bilibili_watch"].run(ToolContext("123456", "654321"),
                                                 {"link": "https://v.douyin.com/abc/"}))


def test_pick_keeps_best_and_varies_rest():
    from mytest.plugins.tools_api_plugin.bilibili import pick

    videos = [bili.Video(bvid=f"BV1{i:09d}", title=str(i)) for i in range(12)]
    picked = pick(videos, 3, random.Random(1))
    assert picked[0] is videos[0] and len(picked) == 3 and all(v in videos[:10] for v in picked)
    assert pick(videos[:2], 3) == videos[:2]


def test_rinne_tools_declares_bilibili_tools():
    pytest.importorskip("maibot_sdk")
    import importlib.util
    import sys
    from pathlib import Path

    from maibot_sdk.components import collect_components

    path = Path(__file__).resolve().parents[1] / "maibot_plugins/rinne_tools/plugin.py"
    spec = importlib.util.spec_from_file_location("rinne_tools_plugin_bili", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    names = {c.get("name") for c in collect_components(mod.RinneToolsPlugin())}
    assert {"rinne_bilibili_search", "rinne_bilibili_watch"} <= names
    assert mod.ToolsSection().bilibili is True
