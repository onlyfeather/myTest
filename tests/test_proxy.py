"""网络代理：公共代理 / 备用代理，各服务的连接方式。运行：python -m pytest tests/test_proxy.py -q"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from mytest.plugins._shared import proxy
from mytest.plugins._shared.settings.registry import SettingField, SettingSection, SettingsStore

MAIN, BACKUP, OWN = "http://mihomo:7890", "http://10.0.0.2:7891", "http://own:3128"


D = proxy.NO_PROXY


@pytest.mark.parametrize("mode, custom, expected", [
    ("auto", OWN, OWN),           # 升级前填过代理：继续用
    ("auto", "", ""),             # 升级前没填：照旧（按环境变量），不会因为填了公共代理就突然走公共代理
    ("direct", OWN, D),           # 真正直连，连环境变量里的代理也不用
    ("main", OWN, MAIN),
    ("backup", "", BACKUP),
    ("custom", OWN, OWN),
    ("custom", "", D),
    ("不认识的", OWN, OWN),        # 配置文件被手改坏了：按老行为
])
def test_resolve(mode, custom, expected):
    assert proxy.resolve(mode, custom, MAIN, BACKUP) == expected


def test_resolve_strips_spaces_and_empty_main():
    assert proxy.resolve("main", "", f"  {MAIN}  ", "") == MAIN
    assert proxy.resolve("main", OWN, "", BACKUP) == D, "选了公共代理但没填，直连，不悄悄走环境变量"


def test_direct_ignores_env_proxy(monkeypatch):
    """选「直连」时 docker-compose.yml 里的 HTTPS_PROXY 不能生效；不填代理（老行为）时照旧生效。"""
    from mytest.plugins._shared.services.http import http_client

    monkeypatch.setenv("HTTPS_PROXY", "http://mihomo:7890")
    monkeypatch.setenv("HTTP_PROXY", "http://mihomo:7890")

    async def state(p):
        async with http_client(p) as c:
            return c._trust_env, [str(k.pattern) for k in c._mounts]

    trust, mounts = asyncio.run(state(proxy.NO_PROXY))
    assert trust is False and mounts == []
    trust, mounts = asyncio.run(state(""))
    assert trust is True and mounts, "老行为：没填就按环境变量"
    trust, mounts = asyncio.run(state(MAIN))
    assert mounts, "填了代理照常用"


def test_environment_shows_env_proxy(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert proxy.environment() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@mihomo:7890")
    monkeypatch.setenv("NO_PROXY", "localhost,qq.com")
    assert proxy.environment() == {"HTTPS_PROXY": "http://mihomo:7890", "NO_PROXY": "localhost,qq.com"}


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
    assert proxy.describe(proxy.NO_PROXY) == "直连"
    assert proxy.describe("") == "按容器环境变量"
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


def test_novelai_and_gpt_image_follow_draw_unless_set(tmp_path, monkeypatch):
    """NovelAI、GPT Image 默认跟「AI 画图」一样；单独设了就用自己的（比如 GPT 中转站直连、NovelAI 走代理）。"""
    from mytest.plugins._shared import settings as shared

    store = SettingsStore(tmp_path / "s.json", env_lookup=lambda name: None)
    monkeypatch.setattr(shared, "proxy_settings", store.register(proxy.section()))
    draw = store.register(SettingSection("draw", "画图", fields=[proxy.mode_field(), proxy.custom_field()]))
    nai = store.register(SettingSection("nai", "NovelAI", fields=[proxy.inherit_mode_field("画图"), proxy.custom_field()]))
    gpt = store.register(SettingSection("gpt", "GPT", fields=[proxy.inherit_mode_field("画图"), proxy.custom_field()]))

    store.update("draw", {"proxy": OWN})  # 升级前的老配置：画图统一走这个代理
    assert shared.proxy_for(nai, draw) == OWN and shared.proxy_for(gpt, draw) == OWN, "默认跟以前一样"

    store.update("gpt", {"proxy_mode": "direct"})
    store.update("proxy", {"main": MAIN})
    store.update("nai", {"proxy_mode": "main"})
    assert shared.proxy_for(gpt, draw) == proxy.NO_PROXY, "GPT 中转站真正直连"
    assert shared.proxy_for(nai, draw) == MAIN, "NovelAI 走公共代理"
    assert shared.proxy_for(draw) == OWN, "画图页自己的设置不受影响"
    assert shared.proxy_for(nai) == MAIN
    store.update("nai", {"proxy_mode": "inherit"})
    assert shared.proxy_for(nai) == "", "没给上级时按老行为（环境变量），不报错"
    modes = [v for v, _ in nai._store._sections["nai"].fields[0].options]
    assert modes[0] == "inherit" and "auto" not in modes


def test_services_have_mode_field():
    """大模型、画图、语音合成都能选连接方式，原来的「代理」键名不变（保存的值继续有效）。"""
    from mytest.plugins._shared.settings import settings_store
    import mytest.plugins.ai_draw_plugin.config  # noqa: F401
    import mytest.plugins.tts_plugin.config  # noqa: F401

    sections = {s.id: s for s in settings_store.sections()}
    assert "proxy" in sections
    for sid in ("llm", "ai_draw", "tts", "novelai", "gpt_image"):
        keys = [f.key for f in sections[sid].fields]
        assert "proxy_mode" in keys and "proxy" in keys, sid
    for sid in ("novelai", "gpt_image"):
        mode = next(f for f in sections[sid].fields if f.key == "proxy_mode")
        assert mode.default == "inherit", sid
