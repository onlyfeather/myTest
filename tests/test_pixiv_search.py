"""Pixiv 搜图（翻页、去重、筛选、按质量挑）、质量评分、屏蔽标签、请求客户端、多页打包。

运行：python -m pytest tests/test_pixiv_search.py -q
"""

from __future__ import annotations

import asyncio
import io
import random
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mytest.plugins.pixiv_bot_plugin import quality
from mytest.plugins.pixiv_bot_plugin.pixiv.api import PixivAPI
from mytest.plugins.pixiv_bot_plugin.pixiv.errors import PixivError
from mytest.plugins.pixiv_bot_plugin.renderers import gallery
from mytest.plugins.pixiv_bot_plugin.search import SearchRequest, Searcher, search_items
from mytest.plugins.pixiv_bot_plugin.seen import SeenTracker
from mytest.plugins.pixiv_bot_plugin.tag_filter import TagFilter

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 质量评分


def work(views, bookmarks, days):
    return {"viewCount": views, "bookmarkCount": bookmarks, "createDate": (NOW - timedelta(days=days)).isoformat()}


def test_quality_orders_works_sensibly():
    hit = quality.score(work(100_000, 12_000, 700), NOW).total
    good = quality.score(work(20_000, 2_000, 30), NOW).total
    plain = quality.score(work(5_000, 100, 30), NOW).total
    fluke = quality.score(work(10, 5, 10), NOW).total
    assert hit > good > plain
    assert fluke < good, "10 views with 5 bookmarks is not a masterpiece"
    assert 35 <= fluke <= 45, "little data stays near neutral"


def test_quality_has_no_cliff_at_three_days():
    """旧算法在 72 小时前后换一套标准，分数会断崖下跌。"""
    before = quality.score(work(3000, 400, 2.9), NOW).total
    after = quality.score(work(3000, 400, 3.1), NOW).total
    assert abs(before - after) < 3


def test_quality_handles_bad_data():
    assert quality.score({}, NOW).total == pytest.approx(quality.NEUTRAL)
    s = quality.score({"viewCount": "abc", "bookmarkCount": -5, "createDate": "bad"}, NOW)
    assert 0 <= s.total <= 100
    assert quality.score(work(10, 50, 1), NOW).bookmark_rate <= 1, "views lagging behind bookmarks"
    assert quality.score(work(50_000, 5_000, 30), NOW).as_dict()["quality_level"] in "SABCD"


# ---------------------------------------------------------------- 屏蔽标签


def test_tag_filter_matches_whole_tags_not_substrings():
    f = TagFilter.of({"R-18", "成人", "猎奇*"})
    assert f.blocks({"tags": ["r18"]}) and f.blocks({"tags": ["Ｒ－１８"]})
    assert not f.blocks({"tags": ["成人の日"]}), "old fuzzy matching blocked this"
    assert not f.blocks({"tags": ["R-18G以外"]})
    assert f.blocks({"tags": ["猎奇向"]}), "explicit prefix with *"
    assert f.blocks({"tags": ["風景"], "xRestrict": 1}), "Pixiv's own rating counts even without the tag"
    assert not TagFilter.of({"血腥"}).blocks({"tags": ["風景"], "xRestrict": 1})


def test_tag_filter_uses_translations():
    f = TagFilter.of({"血腥"})
    assert f.blocks({"tags": ["流血"]}, {"流血": {"zh": "血腥"}})
    detail = {"tags": {"tags": [{"tag": "流血", "translation": {"en": "blood", "zh": "血腥"}}]}}
    assert f.blocks(detail)


# ---------------------------------------------------------------- 搜索


class FakeAPI:
    """每页 60 个作品，ID = 页码 * 1000 + 序号。收藏数和浏览数按 ID 算，可以单独改。"""

    def __init__(self, pages=5, per_page=60, bookmarks=None, users_tag_empty=False):
        self.pages = pages
        self.per_page = per_page
        self.bookmarks = bookmarks or (lambda i: 1000)
        self.users_tag_empty = users_tag_empty
        self.requested: list[tuple[str, int, str]] = []
        self.details = 0

    async def search(self, keyword, page=1, *, order="date_d", **_):
        self.requested.append((keyword, page, order))
        if self.users_tag_empty and "users入り" in keyword:
            return {"illustManga": {"data": [], "total": 0, "lastPage": 1}}
        data = [{"id": str(page * 1000 + n), "tags": ["風景"], "xRestrict": 0} for n in range(self.per_page)]
        if page > self.pages:
            data = []
        return {"illustManga": {"data": data, "total": self.pages * self.per_page, "lastPage": self.pages}}

    async def illust(self, illust_id):
        self.details += 1
        i = int(illust_id)
        return {"id": illust_id, "bookmarkCount": self.bookmarks(i), "viewCount": 10_000,
                "createDate": (NOW - timedelta(days=30)).isoformat(), "tags": {"tags": [{"tag": "風景"}]}}


def searcher(api, blocked=(), seed=1):
    return Searcher(api, lambda: TagFilter.of(set(blocked)), rng=random.Random(seed))


def test_search_items_merges_popular_and_skips_ads():
    body = {"illustManga": {"data": [{"id": "1"}, {"isAdContainer": True}], "total": 120},
            "popular": {"permanent": [{"id": "1"}, {"id": "2"}], "recent": [{"id": "3"}]}}
    items, total, last_page = search_items(body)
    assert [i["id"] for i in items] == ["1", "2", "3"] and total == 120 and last_page == 2


def test_random_search_spreads_over_pages():
    """旧实现每次只看第 1 页，同一个标签来回就是那几十张。"""
    api = FakeAPI(pages=300)
    s = searcher(api, seed=5)
    found = set()
    for _ in range(12):
        out = run(s.search(SearchRequest("風景", count=1)))
        found |= {i["id"] for i in out.images}
    pages = {int(i) // 1000 for i in found}
    assert len(pages) >= 8, f"results come from many pages: {sorted(pages)}"
    assert max(pages) > 10, "goes far past the old 10-page limit"
    assert max(pages) <= 100, "but stays within the default range"
    assert api.requested[0][1] == 1 and api.requested[1][1] != 1, "first search learns the page count, then jumps"


def test_without_premium_sorts_by_date_and_page_range_is_configurable():
    api = FakeAPI(pages=900)
    run(Searcher(api, lambda: TagFilter(), rng=random.Random(1), max_page=lambda: 500).search(SearchRequest("風景")))
    assert {order for _, _, order in api.requested} == {"date_d"}, "popular_d needs premium"
    deep = FakeAPI(pages=900)
    s = Searcher(deep, lambda: TagFilter(), rng=random.Random(3), max_page=lambda: 500)
    for _ in range(10):
        run(s.search(SearchRequest("風景")))
    assert max(p for _, p, _ in deep.requested) > 100 and max(p for _, p, _ in deep.requested) <= 500
    premium = FakeAPI(pages=3)
    run(Searcher(premium, lambda: TagFilter(), premium=lambda: True).search(SearchRequest("風景")))
    assert premium.requested[0][2] == "popular_d"


def test_stale_page_count_falls_back_to_page_one():
    api = FakeAPI(pages=80)
    s = searcher(api, seed=2)
    run(s.search(SearchRequest("風景")))
    api.pages = 1  # 作品被删得只剩一页
    out = run(s.search(SearchRequest("風景")))
    assert out.images and api.requested[-1][1] == 1


def test_bookmark_tags_use_pixiv_levels():
    from mytest.plugins.pixiv_bot_plugin.search import bookmark_keyword, bookmark_tag

    assert bookmark_tag(2000) == 1000 and bookmark_tag(500) == 500 and bookmark_tag(50) == 0
    assert bookmark_keyword("風景", 2500) == "風景 1000users入り"
    assert bookmark_keyword("風景", 0) == "風景"


def test_latest_mode_walks_forward_in_time_and_stops_at_the_cutoff():
    class Timeline(FakeAPI):
        """第 n 页的作品是 n 天前发的；ID 越大越新。"""

        async def search(self, keyword, page=1, *, order="date_d", **_):
            self.requested.append((keyword, page, order))
            if page > self.pages:
                return {"illustManga": {"data": [], "total": 0, "lastPage": self.pages}}
            # 每页 20 张，第 n 页覆盖 (n-1) 天前到 n 天前之间的这一天（每张隔 1 小时，第 1 张是整点后 30 分）
            data = [{"id": str(100_000 - page * 100 - n), "tags": ["風景"], "xRestrict": 0,
                     "createDate": (NOW - timedelta(days=page - 1, hours=n, minutes=30)).isoformat()}
                    for n in range(20)]
            return {"illustManga": {"data": data, "total": self.pages * 20, "lastPage": self.pages}}

    api = Timeline(pages=10, bookmarks=lambda i: 100)
    s = Searcher(api, lambda: TagFilter(), rng=random.Random(1), clock=lambda: NOW)
    # 48 小时内只有前两页的 40 张；要 50 张时翻到第 3 页发现全超出范围就停，不会一直翻到第 10 页
    out = run(s.search(SearchRequest("風景", count=50, mode="recent", min_bookmarks=50, max_age_hours=48,
                                     max_rounds=6, detail_batch=30)))
    assert [p for _, p, _ in api.requested] == [1, 2, 3], "stops once a whole page is too old"
    ids = [int(i["id"]) for i in out.images]
    assert len(ids) == 40 and ids == sorted(ids, reverse=True), "everything in range, newest first"
    assert all("users入り" not in k for k, _, _ in api.requested), "new works have no bookmark tags yet"
    empty = run(Searcher(Timeline(pages=3), lambda: TagFilter(), clock=lambda: NOW + timedelta(days=30)).search(
        SearchRequest("風景", mode="recent", max_age_hours=24)))
    assert empty.reason == "no_recent_images"


def test_search_skips_seen_works_and_never_repeats_in_one_batch():
    api = FakeAPI(pages=1, per_page=8)
    seen = {str(1000 + n) for n in range(5)}
    out = run(searcher(api).search(SearchRequest("風景", count=3, exclude=seen)))
    ids = [i["id"] for i in out.images]
    assert len(ids) == 3 and len(set(ids)) == 3 and not set(ids) & seen


def test_search_reports_all_seen():
    api = FakeAPI(pages=1, per_page=4)
    out = run(searcher(api).search(SearchRequest("風景", exclude={str(1000 + n) for n in range(4)})))
    assert out.images == [] and out.reason == "all_seen"


def test_bookmark_tag_falls_back_to_real_bookmark_counts():
    api = FakeAPI(users_tag_empty=True, bookmarks=lambda i: 800 if i % 2 else 50)
    out = run(searcher(api).search(SearchRequest("くすぐり", count=2, min_bookmarks=500)))
    assert len(out.images) == 2 and all(i["bookmarkCount"] >= 500 for i in out.images)
    assert api.requested[0][0] == "くすぐり 500users入り" and api.requested[1][0] == "くすぐり"


def test_search_pages_further_when_first_page_is_filtered_out():
    """第 1 页全被屏蔽 / 收藏不够时继续翻页，不是直接说搜不到。"""
    api = FakeAPI(pages=4, bookmarks=lambda i: 50 if i < 2000 else 900)
    s = searcher(api, seed=3)
    s._last_pages[("風景 500users入り", "date_d")] = 4
    s.rng.randint = lambda a, b: 1  # 从第 1 页开始，第 1 页的作品收藏都不够
    s._pages_to_try = lambda mode, last: [1, 2, 3, 4]
    out = run(s.search(SearchRequest("風景", count=2, min_bookmarks=500)))
    assert len(out.images) == 2 and out.stats["pages"] >= 2
    assert all(int(i["id"]) >= 2000 for i in out.images)


def test_search_reasons_when_nothing_qualifies():
    api = FakeAPI(pages=1, per_page=6, bookmarks=lambda i: 10)
    assert run(searcher(api).search(SearchRequest("風景", min_bookmarks=500))).reason == "low_bookmarks"
    out = run(searcher(FakeAPI(pages=1, per_page=6), blocked={"風景"}).search(SearchRequest("風景")))
    assert out.reason == "all_blocked"
    empty = FakeAPI(pages=0)
    assert run(searcher(empty).search(SearchRequest("不存在的标签"))).reason == "no_results"


def test_quality_threshold_is_a_preference():
    """质量分都不够时用最高的补上，而不是什么都不发。"""
    api = FakeAPI(pages=1, per_page=6, bookmarks=lambda i: 20)
    out = run(searcher(api).search(SearchRequest("風景", count=1, min_quality=99)))
    assert len(out.images) == 1 and out.stats["low_quality"] >= 1


def test_detail_requests_are_bounded():
    api = FakeAPI(pages=10, bookmarks=lambda i: 0)
    run(searcher(api).search(SearchRequest("風景", count=5, min_bookmarks=1)))
    assert api.details <= 4 * 12 and len(api.requested) <= 6


def test_search_service_dedups_per_chat_and_explains_failures(tmp_path, monkeypatch):
    from mytest.plugins.pixiv_bot_plugin import seen as seen_module
    from mytest.plugins.pixiv_bot_plugin.parser.search import build_search_params
    from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
    from mytest.plugins.pixiv_bot_plugin.services import core
    from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

    tracker = SeenTracker()
    monkeypatch.setattr(core, "seen_tracker", tracker)
    _ = seen_module

    api = FakeAPI(pages=1, per_page=4)

    async def original_urls(illust_id):
        return [f"https://img/{illust_id}.png"]

    api.original_urls = original_urls
    api.proxy_url = lambda url: url
    spider = PixivSpider(PixivStorage(tmp_path / "p.db"), api=api)
    spider.library.load()
    spider.searcher.rng = random.Random(0)

    params = build_search_params("風景", 2, "random")
    first = run(core.search_images(spider, params, "group:1"))
    second = run(core.search_images(spider, params, "group:1"))
    other = run(core.search_images(spider, params, "group:2"))
    ids = lambda r: {i["id"] for i in r.images}  # noqa: E731
    assert first.ok and second.ok and not ids(first) & ids(second), "same chat never repeats"
    assert other.ok and len(ids(other)) == 2, "other chats are independent"
    third = run(core.search_images(spider, params, "group:1"))
    assert not third.ok and "最近都发过了" in third.message

    async def expired(*args, **kwargs):
        raise PixivError("cookie_expired")

    api.search = expired
    failed = run(core.search_images(spider, params, "group:3"))
    assert not failed.ok and "Cookie" in failed.message


# ---------------------------------------------------------------- 每日一图


def make_daily(tmp_path, api=None, preferred=("風景", "オリジナル", "空")):
    from mytest.plugins.pixiv_bot_plugin.daily import DailyPicker
    from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage

    api = api or FakeAPI(pages=200)
    storage = PixivStorage(tmp_path / "p.db")
    picker = DailyPicker(storage, lambda rng: Searcher(api, lambda: TagFilter(), rng=rng), lambda: set(preferred))
    return picker, api, storage


def test_daily_differs_per_user_and_is_stable_per_day(tmp_path):
    picker, api, _ = make_daily(tmp_path)
    day = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)  # 北京时间 9 点，加 5 小时还是同一天
    picks = {user: run(picker.pick(user, now=day)).image["id"] for user in ("10001", "10002", "10003", "10004")}
    assert len(set(picks.values())) == 4, "different people get different pictures"

    calls = len(api.requested)
    again = run(picker.pick("10001", now=day + timedelta(hours=5)))
    assert again.image["id"] == picks["10001"] and not again.fresh
    assert len(api.requested) == calls, "the second time is read from the database, no Pixiv request"

    tomorrow = run(picker.pick("10001", now=day + timedelta(days=1)))
    assert tomorrow.fresh and tomorrow.image["id"] != picks["10001"]


def test_daily_seed_is_reproducible_but_saving_keeps_it_fixed(tmp_path):
    """同一天同一个人，换一个数据库从头算也是同一张（种子决定）；存起来是为了搜索结果变化时也不变。"""
    day = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)
    first = run(make_daily(tmp_path / "a")[0].pick("10001", now=day)).image["id"]
    second = run(make_daily(tmp_path / "b")[0].pick("10001", now=day)).image["id"]
    assert first == second

    picker, api, _ = make_daily(tmp_path / "c")
    chosen = run(picker.pick("10001", now=day)).image["id"]
    api.pages = 3  # 搜索结果变了
    assert run(picker.pick("10001", now=day)).image["id"] == chosen


def test_daily_uses_beijing_date_and_avoids_recent_repeats(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.daily import today

    assert today(datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)) == "20261031", "00:30 Beijing time"
    picker, _, storage = make_daily(tmp_path, api=FakeAPI(pages=1, per_page=3))
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ids = [run(picker.pick("10001", now=start + timedelta(days=d))).image["id"] for d in range(3)]
    assert len(set(ids)) == 3, "no repeats within 30 days while there are other pictures"
    assert run(picker.pick("10001", now=start + timedelta(days=3))) is None, "only three pictures exist"


def test_daily_service_messages(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
    from mytest.plugins.pixiv_bot_plugin.services import core
    from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

    api = FakeAPI(pages=50)

    async def original_urls(illust_id):
        return [f"https://img/{illust_id}.png"]

    api.original_urls = original_urls
    api.proxy_url = lambda url: url
    spider = PixivSpider(PixivStorage(tmp_path / "p.db"), api=api)
    spider.library.load()
    first = run(core.daily_recommend(spider, "10001"))
    assert first.ok and first.images[0]["urls"] == [f"https://img/{first.images[0]['id']}.png"]
    assert first.title.startswith("📅 今日推荐")
    again = run(core.daily_recommend(spider, "10001"))
    assert again.images[0]["id"] == first.images[0]["id"] and again.images[0]["urls"] == first.images[0]["urls"]

    spider.library.tags.replace("preferred", [])
    assert "喜好标签" in run(core.daily_recommend(spider, "10002")).message


def test_seen_tracker_expires_and_is_per_scope():
    now = [0.0]
    tracker = SeenTracker(clock=lambda: now[0])
    tracker.mark("group:1", ["a", "b"])
    assert tracker.seen("group:1") == {"a", "b"} and tracker.seen("group:2") == set()
    assert tracker.seen("") == set()
    now[0] = 4 * 24 * 3600
    assert tracker.seen("group:1") == set()


# ---------------------------------------------------------------- 请求客户端


def make_api(handler, cookie="PHPSESSID=42_abc", **kwargs):
    api = PixivAPI(lambda: cookie, transport=httpx.MockTransport(handler), min_interval=0, **kwargs)
    run(api.start())
    return api


def test_api_sends_cookie_and_returns_body():
    seen = {}

    def handler(request):
        seen["cookie"] = request.headers.get("cookie")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"error": False, "body": {"id": "7", "title": "t"}})

    api = make_api(handler)
    assert run(api.illust("7")) == {"id": "7", "title": "t"}
    assert seen["cookie"] == "PHPSESSID=42_abc" and "lang=zh" in seen["url"]
    assert api.user_id == "42"


@pytest.mark.parametrize("status,body,reason", [
    (403, {"error": True, "message": "forbidden"}, "cookie_expired"),
    (429, "", "rate_limited"),
    (503, "", "pixiv_server_error"),
    (404, {"error": True, "message": "该作品已被删除"}, "not_found"),
    (200, {"error": True, "message": "尚无此页"}, "not_found"),
    (200, "<html>", "bad_response"),
])
def test_api_turns_failures_into_reasons(status, body, reason):
    def handler(request):
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    with pytest.raises(PixivError) as info:
        run(make_api(handler).illust("1"))
    assert info.value.reason == reason and str(info.value), "has a message for the user"


def test_api_needs_cookie_and_handles_network_errors():
    with pytest.raises(PixivError, match="Cookie") as info:
        run(make_api(lambda r: httpx.Response(200), cookie="").illust("1"))
    assert info.value.reason == "missing_cookie"

    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PixivError) as info:
        run(make_api(boom).illust("1"))
    assert info.value.reason == "network_error"


def test_api_original_urls_use_proxy_and_cache():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        pages = [{"urls": {"original": f"https://i.pximg.net/img/1_p{n}.png"}} for n in range(3)]
        return httpx.Response(200, json={"error": False, "body": pages})

    api = make_api(handler, image_host=lambda: "i.example.net")
    urls = run(api.original_urls("1"))
    assert urls[0] == "https://i.example.net/img/1_p0.png" and len(urls) == 3
    run(api.original_urls("1"))
    assert calls == ["/ajax/illust/1/pages"], "cached"


def test_api_search_encodes_keyword():
    seen = {}

    def handler(request):
        seen["path"] = request.url.raw_path.decode()
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"error": False, "body": {"illustManga": {"data": []}}})

    run(make_api(handler).search("初音 ミク/1000users入り", 3, order="popular_d"))
    assert "/" not in seen["path"].removeprefix("/ajax/search/artworks/")
    assert seen["params"]["p"] == "3" and seen["params"]["order"] == "popular_d"


def test_api_check_login():
    def handler(request):
        if request.url.path == "/ajax/user/extra":
            return httpx.Response(200, json={"error": False, "body": {}})
        return httpx.Response(200, json={"error": False, "body": {"userId": "42", "name": "小号"}})

    assert run(make_api(handler).check_login()) == "Cookie 有效，登录的账号：小号（ID 42）"

    def expired(request):
        return httpx.Response(400, json={"error": True, "message": "需要登录"})

    with pytest.raises(PixivError) as info:
        run(make_api(expired).check_login())
    assert info.value.reason == "cookie_expired"


# ---------------------------------------------------------------- 多页打包


def test_gallery_zip_names_and_content():
    assert gallery.safe_name('a/b\\c:d*e?"<>|') == "a_b_c_d_e"
    assert gallery.safe_name("   ") == "pixiv"
    assert gallery.page_name(3, "https://i.example.net/x/1_p2.png", 120) == "p003.png"
    assert gallery.page_name(1, "https://i.example.net/x/1_p0", 6) == "p01.jpg"
    assert gallery.archive_name({"id": 9, "title": "合集/第一话"}) == "pixiv_9_合集_第一话.zip"

    data = gallery.build_zip([("p01.png", b"one"), ("p02.png", b"two")])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["p01.png", "p02.png"] and zf.read("p02.png") == b"two"


def test_gallery_archive_is_sent_for_long_works(monkeypatch):
    from types import SimpleNamespace

    from mytest.plugins.pixiv_bot_plugin.renderers import image_message

    sent, uploads = [], []

    class Msg:
        def __init__(self, parts):
            self.parts = parts

        def __add__(self, other):
            return Msg(self.parts + (other.parts if isinstance(other, Msg) else [other]))

        async def send(self, target=None):
            sent.append((self.parts, target))

    monkeypatch.setattr(image_message, "UniMessage", SimpleNamespace(text=lambda t: Msg([t])))
    monkeypatch.setattr(image_message, "Image", lambda raw, name: ("image", name))

    async def fake_download(url, retries=3):
        return SimpleNamespace(raw=url.encode())

    async def fake_upload(target, name, data):
        uploads.append((target, name, zipfile.ZipFile(io.BytesIO(data)).namelist()))

    monkeypatch.setattr(image_message, "_download_image_for_send", fake_download)
    monkeypatch.setattr(image_message, "_upload_file", fake_upload)

    urls = [f"https://i.example.net/1_p{n}.png" for n in range(8)]
    image = {"id": "1", "title": "合集", "urls": urls, "pageCount": 8}
    run(image_message.send_images_with_info([image], "标题", target="T"))
    assert len(uploads) == 1 and uploads[0][1] == "pixiv_1_合集.zip" and len(uploads[0][2]) == 8
    previews = [p for p in sent[0][0] if isinstance(p, tuple)]
    assert len(sent) == 1 and previews == [("image", f"p0{n}.png") for n in range(1, 6)], "first five as preview"
    assert "共 8 页" in sent[0][0][-1] and "前 5 页预览" in sent[0][0][-1]

    sent.clear()
    uploads.clear()

    async def broken_upload(target, name, data):
        raise RuntimeError("NapCat 不支持")

    monkeypatch.setattr(image_message, "_upload_file", broken_upload)
    run(image_message.send_images_with_info([image], "标题", target="T"))
    assert len(sent) == 2, "falls back to sending the first pages after the cover"
    # 逐张发的路径直接用下载结果当图片段（这里是假的下载结果）
    fallback = [p for p in sent[-1][0] if not isinstance(p, str)]
    assert len(fallback) == gallery.MAX_INLINE_PAGES, sent[-1][0]
    assert any("共 8 页" in p for p in sent[-1][0] if isinstance(p, str))
