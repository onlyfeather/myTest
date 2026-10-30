"""关注作者推送、改图（图生图）、画图记录、运行状态页。运行：python -m pytest tests/test_features.py -q"""

from __future__ import annotations

import asyncio
import io
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mytest.plugins._shared.services import images, novelai
from mytest.plugins._shared.settings import SettingsStore
from mytest.plugins._shared.status import EventLog, StatusRegistry, StatusRow, ago
from mytest.plugins.ai_draw_plugin.history import TTL, DrawHistory, LastDraw
from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
from mytest.plugins.pixiv_bot_plugin.services.follow_push import check_author
from mytest.plugins.webui_plugin.server import LoginGuard, create_router

# ---------------------------------------------------------------- 关注推送


class FakeSpider:
    def __init__(self, storage: PixivStorage, illusts: dict[str, dict], blocked: set[str] = frozenset()):
        self.storage = storage
        self.illusts = illusts
        self.blocked = blocked
        self.favorite_authors = {"42": {"name": "画师"}}

    async def get_author_profile(self, user_id):
        return {"illusts": self.illusts} if self.illusts is not None else None

    def is_blocked(self, illust, *, allow_r18=True):
        if not allow_r18 and int(illust.get("xRestrict") or 0) > 0:
            return True
        return bool(set(illust.get("tags") or []) & self.blocked)

    async def get_illust_original_urls(self, illust_id):
        return [f"https://img/{illust_id}.png"]


def illust(i: int, **extra) -> dict:
    return {"id": str(i), "title": f"t{i}", "tags": extra.pop("tags", []), "userName": "画师", **extra}


def run(coro):
    return asyncio.run(coro)


def test_first_check_only_records(tmp_path):
    spider = FakeSpider(PixivStorage(tmp_path / "p.db"), {"10": illust(10), "12": illust(12)})
    update = run(check_author(spider, "42", max_images=3, allow_r18=False))
    assert update.first_check and update.images == []
    assert spider.storage.get_author_seen("42") == 12


def test_pushes_new_works_oldest_first_with_limit_and_filters(tmp_path):
    storage = PixivStorage(tmp_path / "p.db")
    storage.set_author_seen("42", 12)
    works = {str(i): illust(i) for i in (10, 12, 13, 14, 15, 16, 17)}
    works["14"] = illust(14, xRestrict=1)
    works["15"] = illust(15, tags=["猎奇"])
    spider = FakeSpider(storage, works, blocked={"猎奇"})
    update = run(check_author(spider, "42", max_images=2, allow_r18=False))
    assert [i["id"] for i in update.images] == ["16", "17"]
    assert update.skipped == 1, "13 was new but over the limit; 14 (R-18) and 15 (blocked) are not counted"
    assert storage.get_author_seen("42") == 17

    again = run(check_author(spider, "42", max_images=2, allow_r18=False))
    assert again.images == [] and again.skipped == 0


def test_r18_allowed_and_profile_failure(tmp_path):
    storage = PixivStorage(tmp_path / "p.db")
    storage.set_author_seen("42", 1)
    spider = FakeSpider(storage, {"2": illust(2, xRestrict=1)})
    assert [i["id"] for i in run(check_author(spider, "42", max_images=3, allow_r18=True)).images] == ["2"]
    spider.illusts = None
    assert run(check_author(spider, "42", max_images=3, allow_r18=True)) is None


def test_seen_survives_favorite_author_rewrite(tmp_path):
    storage = PixivStorage(tmp_path / "p.db")
    storage.replace_favorite_authors({"42": {"name": "a"}})
    storage.set_author_seen("42", 99)
    storage.replace_favorite_authors({"42": {"name": "b"}})
    assert storage.get_author_seen("42") == 99


# ---------------------------------------------------------------- 改图


def png(width: int, height: int) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 10, 10)).save(out, format="PNG")
    return out.getvalue()


def test_img2img_payload():
    req = novelai.NovelAIRequest(
        prompt="1girl", negative="bad", model="nai-diffusion-4-5-full", width=832, height=1216,
        steps=28, scale=5, sampler="k_euler", seed=7, source=novelai.Img2Img(image=b"PNG", strength=0.4, noise=0.1),
    )
    payload = novelai.build_payload(req)
    p = payload["parameters"]
    assert payload["action"] == "img2img"
    assert (p["strength"], p["noise"], p["extra_noise_seed"]) == (0.4, 0.1, 7)
    assert p["image"] == "UE5H"
    plain = novelai.build_payload(novelai.NovelAIRequest(**{**req.__dict__, "source": None}))
    assert plain["action"] == "generate" and "image" not in plain["parameters"]


def test_cover_picks_closest_size_and_fills():
    from PIL import Image

    data, name = images.cover(png(1600, 900), novelai.SIZES)
    assert name == "横图"
    img = Image.open(io.BytesIO(data))
    assert img.size == novelai.SIZES["横图"]
    assert img.getpixel((0, 0)) == (200, 10, 10), "no black borders"
    assert images.cover(png(500, 800), novelai.SIZES)[1] == "竖图"


def test_draw_history_expires_and_is_bounded():
    now = [0.0]
    history = DrawHistory(clock=lambda: now[0])
    key = DrawHistory.key("1", "2")
    history.remember(key, LastDraw("novelai", "竖图", "1girl", b"img"))
    assert history.get(key).prompt == "1girl"
    assert history.get(DrawHistory.key("1", None)) is None, "group and private are separate"
    now[0] = TTL + 1
    assert history.get(key) is None
    for i in range(40):
        history.remember(str(i), LastDraw("gpt", "方图", "x", b""))
    assert history.get("0") is None and history.get("39") is not None


def test_draw_novelai_uses_source_size(monkeypatch):
    from types import SimpleNamespace

    from mytest.plugins.ai_draw_plugin import service
    from mytest.plugins.ai_draw_plugin.prompting import DrawRequest

    sent = []

    async def fake_generate(req, **kwargs):
        sent.append(req)
        return b"out", 1

    monkeypatch.setattr(service.novelai, "generate", fake_generate)
    monkeypatch.setattr(service, "novelai_settings", SimpleNamespace(
        translate=True, default_artists="", positive_prefix="best quality", negative="", model="nai-diffusion-4-5-full",
        steps=28, scale=5.0, sampler="k_euler", token="t", base_url="", timeout=60.0,
        img2img_strength=0.5, img2img_noise=0.0, reference_strength=1.0, reference_fidelity=1.0,
    ))
    monkeypatch.setattr(service, "draw_settings", SimpleNamespace(proxy=""))
    result = run(service.draw("novelai", DrawRequest(size="竖图", prompt="白发, 1girl"),
                              service.DrawInputs(source=png(1600, 900), translated=True)))
    assert result.size == "横图"
    assert (sent[0].width, sent[0].height) == novelai.SIZES["横图"]
    assert sent[0].source is not None and sent[0].prompt.endswith("白发, 1girl"), "translated prompt is not re-translated"


# ---------------------------------------------------------------- 运行状态


def test_status_registry_survives_broken_provider():
    registry = StatusRegistry()
    registry.register("b", "B", lambda: [StatusRow("x", "y", "ok"), StatusRow("z", "w", "weird")], order=2)
    registry.register("a", "A", lambda: 1 / 0, order=1)
    snap = registry.snapshot()
    assert [s["id"] for s in snap] == ["a", "b"]
    assert "ZeroDivisionError" in snap[0]["error"]
    assert snap[1]["rows"][1]["level"] == ""


def test_event_log_keeps_recent_newest_first():
    log = EventLog(limit=3, clock=lambda: 1.0)
    for i in range(5):
        log.record("tools", "ok", "draw", f"第 {i} 次\n换行")
    items = log.recent("tools")
    assert [e["message"] for e in items] == ["第 4 次 换行", "第 3 次 换行", "第 2 次 换行"]
    assert log.recent("log") == []


def test_ago():
    assert ago(0) == "从未"
    assert ago(100, now=130) == "刚刚"
    assert ago(0.5, now=3600 * 5) == "4 小时前"


def test_webui_status_endpoint(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    app = FastAPI()
    app.include_router(create_router(store, lambda: "correct-horse-battery", guard=LoginGuard(),
                                     status=lambda: {"sections": [{"id": "x", "rows": []}], "logs": [], "tools": []}))
    client = TestClient(app)
    assert client.get("/webui/api/status").status_code == 401
    client.post("/webui/api/login", json={"password": "correct-horse-battery"},
                headers={"X-Requested-With": "mytest-webui"})
    data = client.get("/webui/api/status").json()
    assert data["ok"] is True and data["sections"][0]["id"] == "x"


# ---------------------------------------------------------------- WebUI 列表管理


def make_manager_app(tmp_path):
    from mytest.plugins._shared.settings import ItemManager, ManagerAction, SettingSection

    store = SettingsStore(tmp_path / "settings.json")
    store.register(SettingSection(id="demo", title="演示"))
    calls = []

    async def items():
        return {"items": [{"id": "1", "title": "甲", "link": "javascript:alert(1)", "values": {"tags": ["a"]}},
                          {"title": "没有 id"}], "notice": "提示"}

    async def run(action, item_id, value):
        calls.append((action, item_id, value))
        if value == ["坏"]:
            raise ValueError("不能是坏")
        return "好了"

    store.set_manager("demo", ItemManager(
        title="列表", help="", items=items, run=run,
        actions=(ManagerAction("add", "添加", scope="global", input="lines"),
                 ManagerAction("remove", "删除"),
                 ManagerAction("rename", "改名", input="text")),
    ))
    app = FastAPI()
    app.include_router(create_router(store, lambda: "correct-horse-battery", guard=LoginGuard()))
    client = TestClient(app)
    head = {"X-Requested-With": "mytest-webui"}
    client.post("/webui/api/login", json={"password": "correct-horse-battery"}, headers=head)
    return client, head, calls, store


def test_manager_items_are_sanitized(tmp_path):
    client, head, _, store = make_manager_app(tmp_path)
    described = next(s for s in store.describe() if s["id"] == "demo")["manager"]
    assert [a["name"] for a in described["actions"]] == ["add", "remove", "rename"]
    assert client.post("/webui/api/manager/demo/items").status_code == 403, "needs the custom header"
    data = client.post("/webui/api/manager/demo/items", headers=head).json()
    assert data["notice"] == "提示"
    assert data["items"] == [{"id": "1", "title": "甲", "detail": "", "link": "", "badges": [],
                              "values": {"tags": ["a"]}}]
    assert client.post("/webui/api/manager/nope/items", headers=head).status_code == 404


def test_manager_actions_validate_scope_and_input(tmp_path):
    client, head, calls, _ = make_manager_app(tmp_path)

    def act(name, **body):
        return client.post(f"/webui/api/manager/demo/action/{name}", json=body, headers=head)

    assert act("add", id="ignored", value="x\n\n y ").json() == {"ok": True, "message": "好了"}
    assert calls[-1] == ("add", "", ["x", "y"]), "global actions get no item id; lines are cleaned"
    assert act("remove", value="ignored").json()["ok"] is False, "item actions need an id"
    act("remove", id="1", value="ignored")
    assert calls[-1] == ("remove", "1", None)
    assert act("rename", id="1", value=["list"]).json()["message"] == "请填写内容"
    assert act("add", value="坏").json() == {"ok": False, "message": "不能是坏"}
    assert act("drop_table", id="1").status_code == 404


# ---------------------------------------------------------------- Pixiv 关注列表


class ManageSpider:
    def __init__(self, storage, *, logged_in=True, names=None, following=None):
        from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

        real = PixivSpider(storage, cookie_source=lambda: "PHPSESSID=1_abc" if logged_in else "")
        real.library.load()
        self.real = real
        self.names = names or {}
        self.following = following or {}
        self.pages = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def get_user_info(self, user_id=None):
        name = self.names.get(user_id)
        return {"error": False, "body": {"userId": user_id, "name": name}} if name else {"error": True}

    async def get_following(self, user_id, *, offset=0, limit=100, rest="show"):
        self.pages.append((rest, offset))
        users = self.following.get(rest, [])
        return {"users": users[offset:offset + limit], "total": len(users)}


def test_parse_author_ids():
    from mytest.plugins.pixiv_bot_plugin.services.follow_manage import parse_author_ids

    ids, bad = parse_author_ids(["123", " https://www.pixiv.net/users/456 ", "https://www.pixiv.net/en/users/789/artworks",
                                 "https://www.pixiv.net/member.php?id=11", "123", "画师名", ""])
    assert ids == ["123", "456", "789", "11"] and bad == ["画师名"]


def test_add_remove_and_aliases(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.services import follow_manage

    storage = PixivStorage(tmp_path / "p.db")
    spider = ManageSpider(storage, names={"123": "画师甲", "456": "画师乙"})
    message = run(follow_manage.run_action(spider, "add", "", ["123", "https://www.pixiv.net/users/456", "999", "乱写"]))
    assert "添加了 2 个" in message and "999" in message and "乱写" in message
    assert spider.favorite_authors["123"]["name"] == "画师甲"
    assert spider.search_author_by_alias("画师甲") == "123", "name becomes an alias"
    assert "本来就关注了" in run(follow_manage.run_action(spider, "add", "", ["123"]))

    run(follow_manage.run_action(spider, "aliases", "123", ["甲", "画师甲", "甲"]))
    assert spider.favorite_authors["123"]["aliases"] == ["甲", "画师甲"]
    import pytest

    with pytest.raises(ValueError, match="已经是「画师甲」的了"):
        run(follow_manage.run_action(spider, "aliases", "456", ["甲"]))

    storage.set_author_seen("123", 5)
    run(follow_manage.run_action(spider, "reset_push", "123", None))
    assert storage.get_author_seen("123") is None
    storage.set_author_seen("123", 5)
    run(follow_manage.run_action(spider, "remove", "123", None))
    assert "123" not in spider.favorite_authors and storage.get_author_seen("123") is None
    with pytest.raises(ValueError, match="不在列表"):
        run(follow_manage.run_action(spider, "remove", "123", None))

    reloaded = ManageSpider(storage)
    assert list(reloaded.favorite_authors) == ["456"], "changes are saved to SQLite"


def test_list_items_marks_push_state(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.services.follow_manage import list_items

    storage = PixivStorage(tmp_path / "p.db")
    spider = ManageSpider(storage, logged_in=False)
    spider.add_favorite_authors_bulk([("1", "甲"), ("2", "")])
    storage.set_author_seen("1", 9)
    data = list_items(spider)
    assert "Cookie" in data["notice"]
    by_id = {i["id"]: i for i in data["items"]}
    assert by_id["1"]["badges"] == ["推送已就绪"] and by_id["2"]["badges"] == ["还没检查过"]
    assert by_id["2"]["title"] == "作者 2" and by_id["1"]["link"] == "https://www.pixiv.net/users/1"


def test_import_following_pages_public_and_private(tmp_path, monkeypatch):
    from mytest.plugins.pixiv_bot_plugin.services import follow_manage

    monkeypatch.setattr(follow_manage, "IMPORT_PAGE", 2)
    monkeypatch.setattr(follow_manage, "IMPORT_GAP", 0)
    storage = PixivStorage(tmp_path / "p.db")
    spider = ManageSpider(storage, following={
        "show": [{"userId": str(i), "userName": f"作者{i}"} for i in range(1, 6)],
        "hide": [{"userId": "3", "userName": "作者3"}, {"userId": "9", "userName": "作者9"}],
    })
    spider.add_favorite_authors_bulk([("1", "作者1")])
    message = run(follow_manage.import_following(spider))
    assert message == "Pixiv 上关注了 6 个作者，新加了 5 个"
    assert spider.pages == [("show", 0), ("show", 2), ("show", 4), ("hide", 0)]
    assert set(spider.favorite_authors) == {"1", "2", "3", "4", "5", "9"}

    import pytest

    with pytest.raises(ValueError, match="Cookie"):
        run(follow_manage.import_following(ManageSpider(storage, logged_in=False)))


# ---------------------------------------------------------------- Pixiv Cookie


def test_normalize_cookie_and_account_id():
    from mytest.plugins.pixiv_bot_plugin.cookie import cookie_user_id, normalize_cookie

    assert normalize_cookie(" 12345_AbCd \n") == "PHPSESSID=12345_AbCd"
    assert normalize_cookie("Cookie: a=1; PHPSESSID=99_x;\r\nb=2;") == "a=1; PHPSESSID=99_x; b=2"
    assert normalize_cookie("  ") == ""
    assert "\n" not in normalize_cookie("a=1\nX-Evil: 1") and "\r" not in normalize_cookie("a=1\r\nb=2")
    assert cookie_user_id("a=1; PHPSESSID=12345_AbCd") == "12345"
    assert cookie_user_id("PHPSESSID=abc") is None and cookie_user_id(None) is None


def test_spider_reads_cookie_live(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

    value = [""]
    spider = PixivSpider(PixivStorage(tmp_path / "p.db"), cookie_source=lambda: value[0])
    assert not spider.is_logged_in and spider.user_id is None
    value[0] = "777_secret"
    assert spider.is_logged_in and spider.full_cookie == "PHPSESSID=777_secret" and spider.user_id == "777"
    value[0] = "other=1"
    spider.user_id = "888"
    assert spider.user_id == "888", "falls back to the id found by the login check"


def test_legacy_cookie_moves_to_webui_once(tmp_path):
    from mytest.plugins._shared.settings import SettingField, SettingSection
    from mytest.plugins.pixiv_bot_plugin.cookie import migrate_legacy_cookie

    store = SettingsStore(tmp_path / "settings.json")
    store.register(SettingSection(id="pixiv", title="Pixiv", fields=[SettingField("cookie", "c", type="secret")]))
    storage = PixivStorage(tmp_path / "p.db")
    assert migrate_legacy_cookie(storage, store) is False, "nothing to move"

    storage.save_cookie("PHPSESSID=5_old", "5")
    assert migrate_legacy_cookie(storage, store) is True
    assert store.get("pixiv", "cookie") == "PHPSESSID=5_old" and storage.load_cookie() is None
    assert "5_old" not in json.dumps(store.describe()), "still a secret in the WebUI"

    storage.save_cookie("PHPSESSID=6_stale", "6")
    assert migrate_legacy_cookie(storage, store) is False, "never overwrites what the WebUI has"
    assert store.get("pixiv", "cookie") == "PHPSESSID=5_old"


def test_tool_calls_are_logged_without_chat(monkeypatch):
    from mytest.plugins._shared import status
    from mytest.plugins.tools_api_plugin import server
    from mytest.plugins.tools_api_plugin.tools import ToolError, ToolSpec

    log = EventLog()
    monkeypatch.setattr(server, "event_log", log)
    monkeypatch.setattr(status, "event_log", log)

    async def ok(ctx, args):
        return {"content": "好"}

    async def bad(ctx, args):
        raise ToolError("画图功能已关闭")

    tools = {
        "music_recommend": ToolSpec("music_recommend", "", {"want": {}, "recent_chat": {}}, ok),
        "draw": ToolSpec("draw", "", {"prompt": {}}, bad),
    }
    app = FastAPI()
    token = "t" * 30
    app.include_router(server.create_router(tools, lambda: token))
    client = TestClient(app, client=("127.0.0.1", 5000))
    auth = {"Authorization": f"Bearer {token}"}
    client.post("/tools/music_recommend", headers=auth,
                json={"user_id": "123456", "group_id": "654321",
                      "args": {"want": "安静的", "recent_chat": ["群友的秘密聊天"]}})
    client.post("/tools/draw", headers=auth, json={"user_id": "123456", "args": {"prompt": "猫"}})
    items = log.recent("tools")
    assert items[0]["level"] == "warn" and "画图功能已关闭" in items[0]["message"]
    assert "want=安静的" in items[1]["message"]
    assert "秘密" not in json.dumps(items, ensure_ascii=False)
