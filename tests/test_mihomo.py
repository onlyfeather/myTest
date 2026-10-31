"""mihomo 控制接口：代理组、节点、测速、切换、订阅。用假的控制接口，返回的结构照 mihomo 文档。
运行：python -m pytest tests/test_mihomo.py -q
"""

import asyncio
import json

import httpx
import pytest

from mytest.plugins._shared import mihomo
from mytest.plugins._shared.services.http import ServiceError


def node(name, delay=None, provider="sub-a", kind="Shadowsocks"):
    history = [] if delay is None else [{"time": "2026-10-31T10:00:00Z", "delay": delay}]
    return {"name": name, "type": kind, "history": history, "provider-name": provider}


PROXIES = {"proxies": {
    "DIRECT": node("DIRECT", kind="Direct", provider=""),
    "REJECT": node("REJECT", kind="Reject", provider=""),
    "香港 01": node("香港 01", 120),
    "日本 01": node("日本 01", 0),
    "美国 01": node("美国 01"),
    "节点选择": {"name": "节点选择", "type": "Selector", "now": "香港 01",
               "all": ["自动选择", "香港 01", "日本 01", "美国 01", "DIRECT"], "history": []},
    "自动选择": {"name": "自动选择", "type": "URLTest", "now": "香港 01",
               "all": ["香港 01", "日本 01", "美国 01"], "history": []},
    "隐藏组": {"name": "隐藏组", "type": "Selector", "now": "DIRECT", "all": ["DIRECT"], "hidden": True},
    "GLOBAL": {"name": "GLOBAL", "type": "Selector", "now": "DIRECT", "all": ["DIRECT", "节点选择"]},
}}
PROVIDERS = {"providers": {
    "default": {"name": "default", "vehicleType": "Compatible", "proxies": []},
    "sub-a": {"name": "sub-a", "vehicleType": "HTTP", "updatedAt": "2026-10-31T08:00:00.123+08:00",
              "proxies": [{}, {}, {}],
              "subscriptionInfo": {"Upload": 1024 ** 3, "Download": 2 * 1024 ** 3, "Total": 100 * 1024 ** 3,
                                   "Expire": 1798675200}},
}}


class FakeController:
    def __init__(self, secret=""):
        self.secret = secret
        self.calls = []
        self.selected = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        if self.secret and request.headers.get("authorization") != f"Bearer {self.secret}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path
        if path == "/version":
            return httpx.Response(200, json={"meta": True, "version": "v1.19.0"})
        if path == "/proxies":
            return httpx.Response(200, json=PROXIES)
        if path == "/providers/proxies":
            return httpx.Response(200, json=PROVIDERS)
        if request.method == "PUT" and path.startswith("/proxies/"):
            group = path.removeprefix("/proxies/")
            self.selected[group] = json.loads(request.content)["name"]
            return httpx.Response(204)
        if path.startswith("/group/") and path.endswith("/delay"):
            return httpx.Response(200, json={"香港 01": 98, "美国 01": 210})
        if request.method == "PUT" and path == "/providers/proxies/sub-a":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Resource not found"})


def client(fake, secret=""):
    return mihomo.Mihomo("http://mihomo:9090/", secret, transport=httpx.MockTransport(fake))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 解析


def test_snapshot_parses_groups_nodes_providers():
    snap = run(client(FakeController()).snapshot())
    assert [(g.name, g.type, g.now) for g in snap.groups] == [("节点选择", "Selector", "香港 01"),
                                                               ("自动选择", "URLTest", "香港 01")], "隐藏组和 GLOBAL 不列"
    assert snap.nodes["香港 01"].delay == 120 and snap.nodes["日本 01"].delay == 0 and snap.nodes["美国 01"].delay is None
    assert "DIRECT" not in snap.nodes and "REJECT" not in snap.nodes, "内置的不算节点"
    (p,) = snap.providers
    assert (p.name, p.vehicle, p.count, p.total) == ("sub-a", "HTTP", 3, 100 * 1024 ** 3), "default 不列"


def test_items_for_webui():
    rows = mihomo.items(run(client(FakeController()).snapshot()), lambda ts: "2027-01-01")
    sel, auto, sub = rows
    assert sel["id"] == "g:节点选择" and sel["badges"] == ["代理组", "手动选择"]
    assert "当前：香港 01（120 ms）" in sel["detail"] and "1 个不通" in sel["detail"]
    assert sel["values"]["select"][0] == "香港 01", "下拉框默认选中当前节点"
    assert set(sel["values"]["select"]) == {"自动选择", "香港 01", "日本 01", "美国 01", "DIRECT"}
    assert auto["badges"] == ["代理组", "自动选最快"]
    assert sub["id"] == "p:sub-a" and sub["badges"] == ["订阅"]
    assert "3 个节点" in sub["detail"] and "3.0 GB / 100.0 GB" in sub["detail"] and "2027-01-01" in sub["detail"]
    assert "2026-10-31 08:00:00" in sub["detail"]


def test_fmt():
    assert (mihomo.fmt_delay(None), mihomo.fmt_delay(0), mihomo.fmt_delay(88)) == ("未测速", "不通", "88 ms")
    assert mihomo.fmt_bytes(512) == "512 B" and mihomo.fmt_bytes(1536) == "1.5 KB"


# ---------------------------------------------------------------- 操作


def test_select_test_update():
    fake = FakeController(secret="s3cret")
    c = client(fake, "s3cret")
    assert run(c.version()) == "v1.19.0"
    run(c.select("节点选择", "日本 01"))
    assert fake.selected == {"节点选择": "日本 01"}, "组名里的中文按 URL 编码发出去，接口那边解出来是原名"
    assert run(c.test_group("节点选择")) == {"香港 01": 98, "美国 01": 210}
    method, path, params = fake.calls[-1]
    assert path == "/group/节点选择/delay" and params["timeout"] == "5000" and "generate_204" in params["url"]
    run(c.update_provider("sub-a"))
    assert fake.calls[-1][:2] == ("PUT", "/providers/proxies/sub-a")


def test_pick_node():
    g = mihomo.Group("节点选择", "Selector", "香港 01", ("香港 01", "日本 01"))
    assert mihomo.pick_node(" 日本 01 ", g) == "日本 01"
    with pytest.raises(ValueError, match="不在"):
        mihomo.pick_node("火星 01", g)
    with pytest.raises(ValueError, match="请选择"):
        mihomo.pick_node("", g)


@pytest.mark.parametrize("secret, match", [("wrong", "密钥不对")])
def test_bad_secret(secret, match):
    with pytest.raises(ServiceError, match=match):
        run(client(FakeController(secret="right"), secret).snapshot())


def test_not_found_and_not_json():
    def handler(request):
        if request.url.path == "/version":
            return httpx.Response(200, text="<html>")
        return httpx.Response(404, json={"message": "Resource not found"})

    c = mihomo.Mihomo("http://mihomo:9090", transport=httpx.MockTransport(handler))
    with pytest.raises(ServiceError, match="不是 JSON"):
        run(c.version())
    with pytest.raises(ServiceError, match="找不到"):
        run(c.update_provider("nope"))


def test_connection_refused_explains_external_controller():
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    c = mihomo.Mihomo("http://mihomo:9090", transport=httpx.MockTransport(handler))
    with pytest.raises(ServiceError, match="external-controller.*0.0.0.0:9090"):
        run(c.version())


def test_empty_url():
    with pytest.raises(ServiceError, match="还没有填"):
        run(mihomo.Mihomo("").version())


def test_controller_never_uses_proxy_env(monkeypatch):
    """环境变量里设了代理也不能让控制接口走代理：切到坏节点后还要能切回来。"""
    monkeypatch.setenv("HTTP_PROXY", "http://broken:1")
    monkeypatch.setenv("ALL_PROXY", "http://broken:1")
    c = mihomo.Mihomo("http://mihomo:9090")
    assert c._client()._trust_env is False


# ---------------------------------------------------------------- WebUI 的按钮


@pytest.fixture
def webui(tmp_path, monkeypatch):
    from mytest.plugins._shared import settings as shared
    from mytest.plugins._shared import proxy as proxy_mod
    from mytest.plugins._shared.settings.registry import SettingsStore

    store = SettingsStore(tmp_path / "s.json", env_lookup=lambda name: None)
    view = store.register(proxy_mod.section())
    monkeypatch.setattr(shared, "proxy_settings", view)
    fake = FakeController()
    monkeypatch.setattr(shared, "_mihomo", lambda: client(fake))
    return store, fake, shared


def test_webui_items_and_actions(webui):
    store, fake, shared = webui
    assert "填 mihomo 控制接口" in run(shared._mihomo_items())["notice"]
    store.update("proxy", {"mihomo_url": "http://mihomo:9090"})
    data = run(shared._mihomo_items())
    assert [r["id"] for r in data["items"]] == ["g:节点选择", "g:自动选择", "p:sub-a"] and data["notice"] == ""

    assert "已切换到「日本 01」" in run(shared._mihomo_run("select", "g:节点选择", "日本 01"))
    with pytest.raises(ValueError, match="自动选择的组"):
        run(shared._mihomo_run("select", "g:自动选择", "日本 01"))
    with pytest.raises(ValueError, match="不在"):
        run(shared._mihomo_run("select", "g:节点选择", "火星"))
    # 节点选择里有 3 个节点（子组「自动选择」和 DIRECT 不算）：香港、美国测通，日本不通
    assert "2 个能用，1 个不通" in run(shared._mihomo_run("test", "g:节点选择", None))
    assert "最快：香港 01 98 ms" in run(shared._mihomo_run("test", "g:节点选择", None))
    assert "节点选择：2 个能用" in run(shared._mihomo_run("test_all", "", None))
    assert "订阅「sub-a」已更新" == run(shared._mihomo_run("update", "p:sub-a", None))
    with pytest.raises(ValueError, match="没有用"):
        run(shared._mihomo_run("update", "g:节点选择", None))


def test_webui_notice_when_no_subscription(webui, monkeypatch):
    store, fake, shared = webui
    store.update("proxy", {"mihomo_url": "http://mihomo:9090"})
    monkeypatch.setattr(mihomo, "parse_providers", lambda data: [])
    assert "没有订阅" in run(shared._mihomo_items())["notice"]


def test_controller_url_validation(webui):
    store, _, _ = webui
    assert "http://" in store.update("proxy", {"mihomo_url": "mihomo:9090"})["mihomo_url"]
    assert store.update("proxy", {"mihomo_url": "http://mihomo:9090", "mihomo_secret": "x"}) == {}
