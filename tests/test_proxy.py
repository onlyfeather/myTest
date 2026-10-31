"""网络代理：公共代理 / 备用代理，各服务的连接方式。运行：python -m pytest tests/test_proxy.py -q"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from mytest.plugins._shared import proxy
from mytest.plugins._shared.settings.registry import SettingField, SettingSection, SettingsStore

MAIN, BACKUP, OWN = "http://mihomo:7890", "http://10.0.0.2:7891", "http://own:3128"


@pytest.mark.parametrize("mode, custom, expected", [
    ("auto", OWN, OWN),           # 升级前填过代理：继续用
    ("auto", "", ""),             # 升级前没填：继续直连，不会因为填了公共代理就突然走代理
    ("direct", OWN, ""),
    ("main", OWN, MAIN),
    ("backup", "", BACKUP),
    ("custom", OWN, OWN),
    ("custom", "", ""),
    ("不认识的", OWN, OWN),        # 配置文件被手改坏了：按老行为
])
def test_resolve(mode, custom, expected):
    assert proxy.resolve(mode, custom, MAIN, BACKUP) == expected


def test_resolve_strips_spaces_and_empty_main():
    assert proxy.resolve("main", "", f"  {MAIN}  ", "") == MAIN
    assert proxy.resolve("main", OWN, "", BACKUP) == "", "选了公共代理但没填，按直连"


@pytest.mark.parametrize("url, ok", [
    ("", True),
    ("http://mihomo:7890", True),
    ("https://proxy.example.com:443", True),
    ("http://user:pass@10.0.0.2:7890", True),
    ("socks5://127.0.0.1:1080", False),  # 没装 socksio，提前挡住，免得请求时才报错
    ("mihomo:7890", False),
    ("http://mihomo", False),
    ("http://:7890", False),
    ("ftp://x:21", False),
    ("http://mihomo:99999", False),
])
def test_problem(url, ok):
    assert (proxy.problem(url) == "") is ok, proxy.problem(url)


def test_describe_hides_password():
    assert proxy.describe("") == "直连"
    assert proxy.describe("http://user:secret@10.0.0.2:7890") == "http://10.0.0.2:7890"
    assert "SOCKS" in proxy.problem("socks5://10.0.0.2:1080")


def test_store_rejects_bad_proxy_and_keeps_old_value(tmp_path):
    store = SettingsStore(tmp_path / "s.json", env_lookup=lambda name: None)
    store.register(proxy.section())
    store.register(SettingSection("svc", "服务", fields=[proxy.mode_field(), proxy.custom_field(),
                                                       SettingField("x", "x")]))
    assert store.update("svc", {"proxy": OWN}) == {}
    errors = store.update("svc", {"proxy": "mihomo:7890", "x": "1"})
    assert "proxy" in errors and "开头" in errors["proxy"]
    assert store.get("svc", "proxy") == OWN and store.get("svc", "x") == "", "有错误时什么都不改"
    assert "端口" in store.update("proxy", {"main": "http://mihomo"})["main"]
    assert store.update("proxy", {"main": MAIN, "backup": ""}) == {}
    assert store.get("svc", "proxy_mode") == "auto", "默认跟以前一样"


def test_proxy_for_reads_live_settings(tmp_path, monkeypatch):
    from mytest.plugins._shared import settings as shared

    store = SettingsStore(tmp_path / "s.json", env_lookup=lambda name: None)
    proxy_view = store.register(proxy.section())
    svc = store.register(SettingSection("svc", "服务", fields=[proxy.mode_field(), proxy.custom_field()]))
    monkeypatch.setattr(shared, "proxy_settings", proxy_view)

    assert shared.proxy_for(svc) == ""
    store.update("proxy", {"main": MAIN})
    assert shared.proxy_for(svc) == "", "老配置没选公共代理，照样直连"
    store.update("svc", {"proxy_mode": "main"})
    assert shared.proxy_for(svc) == MAIN, "改了马上生效"
    store.update("proxy", {"main": "http://other:7890"})
    assert shared.proxy_for(svc) == "http://other:7890"

    class Legacy:  # 还没有 proxy_mode 的配置（比如测试里的假配置）
        proxy = OWN
    assert shared.proxy_for(Legacy()) == OWN


def test_probe_reports_each_target(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "fish" in request.url.host:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(404)

    @asynccontextmanager
    async def fake_client(p="", timeout=60.0):
        seen.append(("proxy", p))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    from mytest.plugins._shared.services import http

    monkeypatch.setattr(http, "http_client", fake_client)
    targets = (("A", "https://a.example"), ("Fish Audio", "https://api.fish.audio"))
    results = asyncio.run(proxy.probe(MAIN, targets))
    assert seen[0] == ("proxy", MAIN)
    assert [(r.name, r.ok) for r in results] == [("A", True), ("Fish Audio", False)]
    assert results[0].detail == "HTTP 404" and "连接超时" in results[1].detail
    text = proxy.report("公共代理", MAIN, results)
    assert "公共代理（http://mihomo:7890）" in text and "✓ A" in text and "✗ Fish Audio" in text


def test_services_have_mode_field():
    """大模型、画图、语音合成都能选连接方式，原来的「代理」键名不变（保存的值继续有效）。"""
    from mytest.plugins._shared.settings import settings_store
    import mytest.plugins.ai_draw_plugin.config  # noqa: F401
    import mytest.plugins.tts_plugin.config  # noqa: F401

    sections = {s.id: s for s in settings_store.sections()}
    assert "proxy" in sections
    for sid in ("llm", "ai_draw", "tts"):
        keys = [f.key for f in sections[sid].fields]
        assert "proxy_mode" in keys and "proxy" in keys, sid
