"""配置中心、外部服务客户端、WebUI 接口的测试。

运行：python -m pytest tests -q
外部接口全部用 httpx.MockTransport 模拟，不会真的发请求。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mytest.plugins._shared import limits
from mytest.plugins._shared.services import fish_audio, gpt_image, http, novelai
from mytest.plugins._shared.settings.registry import SettingField, SettingSection, SettingsStore, coerce
from mytest.plugins.webui_plugin.server import LoginGuard, create_router


def make_store(tmp_path: Path, env: dict | None = None) -> SettingsStore:
    env = env or {}
    store = SettingsStore(tmp_path / "settings.json", env_lookup=lambda name: env.get(name))
    store.register(
        SettingSection(
            id="demo",
            title="演示",
            fields=[
                SettingField("key", "密钥", type="secret", env=("demo_key",)),
                SettingField("model", "模型", default="m1", env=("demo_model",)),
                SettingField("on", "开关", type="bool", default=True),
                SettingField("n", "数量", type="int", default=3, minimum=1, maximum=10),
                SettingField("groups", "群", type="lines", default=[], env=("demo_groups",)),
                SettingField("kind", "类型", type="select", default="a", options=(("a", "A"), ("b", "B"))),
            ],
        )
    )
    return store


# ---------------------------------------------------------------- 配置中心


def test_priority_saved_over_env_over_default(tmp_path):
    store = make_store(tmp_path, {"demo_model": "from-env", "demo_groups": "1, 2"})
    assert store.resolve("demo", "model") == ("from-env", "env")
    assert store.get("demo", "groups") == ["1", "2"]
    assert store.resolve("demo", "n") == (3, "default")
    assert store.update("demo", {"model": "saved"}) == {}
    assert store.resolve("demo", "model") == ("saved", "saved")
    store.update("demo", reset=["model"])
    assert store.resolve("demo", "model") == ("from-env", "env")


def test_persisted_and_reloaded(tmp_path):
    store = make_store(tmp_path)
    store.update("demo", {"n": "7", "on": "关", "groups": "123\n\n456 "})
    again = make_store(tmp_path)
    assert again.get("demo", "n") == 7
    assert again.get("demo", "on") is False
    assert again.get("demo", "groups") == ["123", "456"]


def test_invalid_values_rejected_atomically(tmp_path):
    store = make_store(tmp_path)
    errors = store.update("demo", {"n": "99", "kind": "z", "model": "ok", "nope": 1})
    assert set(errors) == {"n", "kind", "nope"}
    assert store.get("demo", "model") == "m1", "nothing is saved when any field is invalid"
    assert not (tmp_path / "settings.json").exists()


def test_secret_never_described(tmp_path):
    store = make_store(tmp_path, {"demo_key": "sk-secret"})
    field = next(f for f in store.describe()[0]["fields"] if f["key"] == "key")
    assert field["value"] == "" and field["has_value"] is True
    assert "sk-secret" not in json.dumps(store.describe())


def test_section_view_reads_live_values(tmp_path):
    store = make_store(tmp_path)
    view = store.section("demo")
    assert view.model == "m1"
    view.set("model", "m2")
    assert view.model == "m2"
    with pytest.raises(AttributeError):
        view.model = "x"


def test_coerce_rules():
    f = SettingField("x", "x", type="float", default=1.0, minimum=0.5, maximum=2)
    assert coerce(f, "1.5") == 1.5
    with pytest.raises(ValueError):
        coerce(f, "3")
    with pytest.raises(ValueError):
        coerce(SettingField("i", "i", type="int", default=1), "1.5")
    with pytest.raises(ValueError):
        coerce(SettingField("i", "i", type="int", default=1), True)
    assert coerce(SettingField("t", "t"), "  a  ") == "a"
    assert coerce(SettingField("t", "t", type="textarea"), " a ") == " a "


# ---------------------------------------------------------------- 限流


def test_limiter():
    now = [100.0]
    lim = limits.UsageLimiter(clock=lambda: now[0])

    async def run():
        assert await lim.try_acquire("u", cooldown=30, max_concurrent=1, exempt=False) == ""
        assert "稍后" in await lim.try_acquire("v", cooldown=30, max_concurrent=1, exempt=False)
        lim.release("u", success=True)
        assert "冷却" in await lim.try_acquire("u", cooldown=30, max_concurrent=1, exempt=False)
        assert await lim.try_acquire("u", cooldown=30, max_concurrent=1, exempt=True) == ""
        lim.release("u", success=False)
        assert await lim.try_acquire("u", cooldown=30, max_concurrent=1, exempt=False) == "", "failure resets cooldown"
        lim.release("u", success=True)
        now[0] += 31
        assert await lim.try_acquire("u", cooldown=30, max_concurrent=1, exempt=False) == ""

    asyncio.run(run())
    assert limits.scope_allowed(None, allowed_groups=[], allow_private=False) is False
    assert limits.scope_allowed("1", allowed_groups=[], allow_private=False) is True
    assert limits.scope_allowed("1", allowed_groups=["2"], allow_private=True) is False


# ---------------------------------------------------------------- 外部服务


@pytest.fixture
def mock_http(monkeypatch):
    """把 http_client 换成 MockTransport；handler 列表决定返回什么。"""
    calls: list[httpx.Request] = []
    responder = {"fn": lambda req: httpx.Response(500)}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_client(proxy="", timeout=60.0):
        def handler(request):
            calls.append(request)
            return responder["fn"](request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client

    for module in (novelai, gpt_image, fish_audio):
        monkeypatch.setattr(module, "http_client", fake_client)
    return calls, responder


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def _zip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("image_0.png", data)
    return buf.getvalue()


def test_novelai_payload_v45():
    payload = novelai.build_payload(
        novelai.NovelAIRequest("1girl", "lowres", "nai-diffusion-4-5-full", 832, 1216, 28, 5.0, "k_euler_ancestral", 7)
    )
    p = payload["parameters"]
    assert payload["action"] == "generate" and payload["input"] == "1girl"
    assert p["seed"] == 7 and p["n_samples"] == 1 and p["uc"] == "lowres"
    assert p["v4_prompt"]["caption"] == {"base_caption": "1girl", "char_captions": []}
    assert p["v4_negative_prompt"]["caption"] == {"base_caption": "lowres", "char_captions": []}
    assert p["params_version"] == 3
    v3 = novelai.build_payload(novelai.NovelAIRequest("a", "b", "nai-diffusion-3", 832, 1216, 28, 5, "k_euler"))
    assert "v4_prompt" not in v3["parameters"]


def test_novelai_payload_v5():
    full = novelai.build_payload(
        novelai.NovelAIRequest("1girl", "lowres", "nai-diffusion-5-full", 832, 1216, 28, 5.0, "k_dpmpp_2m", 1)
    )
    p = full["parameters"]
    assert full["model"] == "nai-diffusion-5-full" and p["params_version"] == 4
    assert p["steps"] == 28 and p["sampler"] == "k_dpmpp_2m" and p["uc"] == "lowres"
    assert p["v4_prompt"]["caption"]["char_captions"] == []
    assert p["v4_negative_prompt"]["caption"] == {"base_caption": "lowres", "char_captions": []}

    medium = novelai.build_payload(
        novelai.NovelAIRequest("1girl", "lowres", novelai.MEDIUM_MODEL, 832, 1216, 28, 5.0, "k_dpmpp_2m", 1)
    )
    m = medium["parameters"]
    assert medium["model"] == "nai-diffusion-5-full-medium" and m["params_version"] == 4
    assert (m["steps"], m["sampler"], m["cfg_rescale"]) == (14, "k_euler_ancestral", 0), "medium constraints enforced"
    assert m["uc"] == "" and m["v4_negative_prompt"]["caption"]["base_caption"] == "", "medium ignores custom negative"
    assert m["v4_prompt"]["caption"]["base_caption"] == "1girl"
    assert {v for v, _ in novelai.MODELS} >= {"nai-diffusion-5-full", "nai-diffusion-5-curated", novelai.MEDIUM_MODEL}


def test_novelai_generate(mock_http):
    calls, responder = mock_http
    responder["fn"] = lambda req: httpx.Response(200, content=_zip(PNG))
    req = novelai.NovelAIRequest("1girl", "", "nai-diffusion-4-5-full", 832, 1216, 28, 5, "k_euler_ancestral")
    image, seed = asyncio.run(novelai.generate(req, token="pst-x", base_url="https://image.novelai.net/"))
    assert image == PNG and isinstance(seed, int)
    assert str(calls[0].url) == "https://image.novelai.net/ai/generate-image"
    assert calls[0].headers["authorization"] == "Bearer pst-x"

    responder["fn"] = lambda req: httpx.Response(402, json={"message": "Not enough Anlas"})
    with pytest.raises(http.ServiceError, match="402.*余额"):
        asyncio.run(novelai.generate(req, token="pst-x", base_url="https://image.novelai.net"))
    with pytest.raises(http.ServiceError, match="Token"):
        asyncio.run(novelai.generate(req, token=" ", base_url="https://image.novelai.net"))


def test_novelai_legacy_host_redirected(mock_http):
    assert novelai.api_root("https://api.novelai.net") == "https://image.novelai.net"
    assert novelai.api_root("https://api.novelai.net/") == "https://image.novelai.net"
    assert novelai.api_root("") == "https://image.novelai.net"
    assert novelai.api_root("https://relay.example/nai/") == "https://relay.example/nai"

    calls, responder = mock_http
    responder["fn"] = lambda req: httpx.Response(200, json={
        "tier": 3, "trainingStepsLeft": {"fixedTrainingStepsLeft": 9898, "purchasedTrainingSteps": 32},
    })
    msg = asyncio.run(novelai.check_subscription(token="pst-x", base_url="https://api.novelai.net"))
    assert str(calls[-1].url) == "https://image.novelai.net/user/subscription"
    assert "Opus" in msg and "9930" in msg

    responder["fn"] = lambda req: httpx.Response(200, content=_zip(PNG))
    req = novelai.NovelAIRequest("1girl", "", "nai-diffusion-4-5-full", 832, 1216, 28, 5, "k_euler_ancestral")
    asyncio.run(novelai.generate(req, token="pst-x", base_url="https://api.novelai.net"))
    assert str(calls[-1].url) == "https://image.novelai.net/ai/generate-image"


def test_gpt_image_generate(mock_http):
    calls, responder = mock_http
    responder["fn"] = lambda req: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    image = asyncio.run(
        gpt_image.generate("一只猫", api_key="sk", base_url="https://api.openai.com/v1", model="gpt-image-2.5-flare",
                           size="1024x1536", quality="auto", moderation="low")
    )
    assert image == PNG
    body = json.loads(calls[0].content)
    assert body == {"model": "gpt-image-2.5-flare", "prompt": "一只猫", "n": 1, "size": "1024x1536",
                    "quality": "auto", "moderation": "low"}

    # 中转站只返回链接
    def url_mode(req):
        if req.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]})
        return httpx.Response(200, content=PNG)

    responder["fn"] = url_mode
    assert asyncio.run(gpt_image.generate("x", api_key="sk", base_url="https://relay/v1", model="m",
                                          size="1024x1024", moderation="")) == PNG
    assert "moderation" not in json.loads(calls[1].content)

    responder["fn"] = lambda req: httpx.Response(400, json={"error": {"message": "rejected by safety system"}})
    with pytest.raises(http.ServiceError, match="审核"):
        asyncio.run(gpt_image.generate("x", api_key="sk", base_url="https://a/v1", model="m", size="1024x1024"))


def test_fish_audio(mock_http):
    calls, responder = mock_http
    responder["fn"] = lambda req: httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"})
    audio = asyncio.run(
        fish_audio.synthesize("你好", api_key="k", base_url="https://api.fish.audio", model="s2.1-pro",
                              reference_id="abc", speed=1.2)
    )
    assert audio == b"ID3audio"
    req = calls[0]
    assert str(req.url) == "https://api.fish.audio/v1/tts" and req.headers["model"] == "s2.1-pro"
    body = json.loads(req.content)
    assert body["reference_id"] == "abc" and body["prosody"]["speed"] == 1.2 and body["format"] == "mp3"

    responder["fn"] = lambda req: httpx.Response(200, json={"error": "x"}, headers={"content-type": "application/json"})
    with pytest.raises(http.ServiceError, match="没有返回音频"):
        asyncio.run(fish_audio.synthesize("你好", api_key="k", base_url="https://x", model="s2.1-pro", reference_id=""))


def test_describe_exception():
    req = httpx.Request("POST", "https://api.fish.audio/v1/tts")
    assert "超时" in http.describe_exception("X", httpx.ReadTimeout("t"))
    assert "代理" in http.describe_exception("X", httpx.ProxyError("p"))
    assert http.describe_exception("X", http.ServiceError("自定义")) == "自定义"
    msg = http.describe_exception("Fish Audio", httpx.ConnectTimeout("timed out", request=req))
    assert "https://api.fish.audio" in msg and "代理" in msg
    msg = http.describe_exception("Fish Audio", httpx.ConnectError("[Errno -2] Name or service not known", request=req))
    assert "域名解析失败" in msg and "Name or service" in msg


def test_fish_api_root():
    for url in ("https://api.fish.audio", "https://api.fish.audio/", "https://api.fish.audio/v1",
                "https://api.fish.audio/v1/tts", " https://api.fish.audio/v1/ "):
        assert fish_audio.api_root(url) == "https://api.fish.audio", url


# ---------------------------------------------------------------- WebUI

HEAD = {"X-Requested-With": "mytest-webui"}


def make_app(tmp_path, password="correct-horse-battery"):
    store = make_store(tmp_path, {"demo_key": "sk-secret"})

    async def tester():
        return "连接正常"

    store.set_tester("demo", tester)
    app = FastAPI()
    app.include_router(create_router(store, lambda: password, guard=LoginGuard()))
    return TestClient(app), store


def test_webui_requires_login_and_hides_secrets(tmp_path):
    client, store = make_app(tmp_path)
    assert client.get("/webui").status_code == 200
    assert client.get("/webui/api/settings").status_code == 401
    assert client.put("/webui/api/settings/demo", json={"values": {}}, headers=HEAD).status_code == 401

    assert client.post("/webui/api/login", json={"password": "correct-horse-battery"}).status_code == 403, \
        "login without the custom header is rejected"
    r = client.post("/webui/api/login", json={"password": "correct-horse-battery"}, headers=HEAD)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    data = client.get("/webui/api/settings").json()
    assert "sk-secret" not in json.dumps(data)

    r = client.put("/webui/api/settings/demo", json={"values": {"n": 11}}, headers=HEAD)
    assert r.status_code == 422 and "n" in r.json()["errors"]
    r = client.put("/webui/api/settings/demo", json={"values": {"n": 5, "key": "sk-new"}}, headers=HEAD)
    assert r.status_code == 200 and store.get("demo", "n") == 5 and store.get("demo", "key") == "sk-new"
    assert "sk-new" not in r.text
    assert client.put("/webui/api/settings/demo", json={"values": {"n": 6}}).status_code == 403, "needs header"

    assert client.post("/webui/api/settings/demo/test", headers=HEAD).json() == {"ok": True, "message": "连接正常"}
    assert client.put("/webui/api/settings/nope", json={"values": {}}, headers=HEAD).status_code == 404

    client.post("/webui/api/logout", headers=HEAD)
    assert client.get("/webui/api/settings").status_code == 401


def test_fetch_options(tmp_path):
    store = SettingsStore(tmp_path / "s.json", env_lookup=lambda name: None)
    store.register(SettingSection(id="x", title="X", fields=[
        SettingField("model", "模型", fetchable=True), SettingField("plain", "普通"),
    ]))

    async def provider():
        return [("b", "B"), ("a", ""), ("b", "dup"), (" ", "blank")]

    with pytest.raises(ValueError):
        store.set_options_provider("x", "plain", provider)
    fields = {f["key"]: f for f in store.describe()[0]["fields"]}
    assert fields["model"]["fetchable"] is False, "no provider registered yet"
    store.set_options_provider("x", "model", provider)
    assert {f["key"]: f for f in store.describe()[0]["fields"]}["model"]["fetchable"] is True
    assert asyncio.run(store.fetch_options("x", "model")) == [{"value": "b", "label": "B"}, {"value": "a", "label": "a"}]
    with pytest.raises(KeyError):
        asyncio.run(store.fetch_options("x", "plain"))


def test_model_list_services(mock_http, monkeypatch):
    from mytest.plugins._shared.services import model_list

    calls, responder = mock_http
    monkeypatch.setattr(model_list, "http_client", novelai.http_client)
    responder["fn"] = lambda req: httpx.Response(200, json={"data": [
        {"id": "text-embedding-3"}, {"id": "gpt-4o"}, {"id": "gpt-image-2.5-flare"}, {"id": "deepseek-chat"},
    ]})
    ids = asyncio.run(model_list.openai_models("大模型", api_key="sk", base_url="https://x/v1/"))
    assert str(calls[0].url) == "https://x/v1/models"
    assert [m for m, _ in model_list.chat_first(ids)][:2] == ["deepseek-chat", "gpt-4o"]
    assert model_list.image_only(ids) == [("gpt-image-2.5-flare", "gpt-image-2.5-flare")]
    assert len(model_list.image_only(["a", "b"])) == 2, "falls back to all when nothing looks like an image model"
    with pytest.raises(http.ServiceError, match="API Key"):
        asyncio.run(model_list.openai_models("大模型", api_key="", base_url="https://x/v1"))
    responder["fn"] = lambda req: httpx.Response(200, text="<html>")
    with pytest.raises(http.ServiceError, match="/v1"):
        asyncio.run(model_list.openai_models("大模型", api_key="sk", base_url="https://x"))

    def fish(req):
        if req.url.params.get("self") == "true":
            return httpx.Response(200, json={"items": [{"_id": "mine1", "title": "凛音"}]})
        return httpx.Response(200, json={"items": [{"_id": "pub1", "title": ""}, {"title": "no id"}]})

    responder["fn"] = fish
    voices = asyncio.run(model_list.fish_voices(api_key="k", base_url="https://api.fish.audio"))
    assert voices == [("mine1", "我的 · 凛音"), ("pub1", "热门 · 未命名")]
    assert calls[-1].url.path == "/model" and calls[-1].headers["authorization"] == "Bearer k"


def test_webui_options_endpoint(tmp_path):
    client, store = make_app(tmp_path)
    store._sections["demo"].fields.append(SettingField("voice", "音色", fetchable=True))

    async def voices():
        return [("v1", "音色一")]

    async def broken():
        raise http.ServiceError("接口挂了")

    store.set_options_provider("demo", "voice", voices)
    url = "/webui/api/settings/demo/options/voice"
    assert client.post(url, headers=HEAD).status_code == 401
    client.post("/webui/api/login", json={"password": "correct-horse-battery"}, headers=HEAD)
    assert client.post(url).status_code == 403
    assert client.post(url, headers=HEAD).json() == {"ok": True, "options": [{"value": "v1", "label": "音色一"}]}
    assert client.post("/webui/api/settings/demo/options/model", headers=HEAD).status_code == 404
    store.set_options_provider("demo", "voice", broken)
    assert client.post(url, headers=HEAD).json() == {"ok": False, "message": "接口挂了"}


def test_webui_lockout_and_disabled(tmp_path):
    client, _ = make_app(tmp_path)
    for _ in range(5):
        assert client.post("/webui/api/login", json={"password": "wrong"}, headers=HEAD).status_code == 401
    r = client.post("/webui/api/login", json={"password": "correct-horse-battery"}, headers=HEAD)
    assert r.status_code == 429, "locked after 5 failures, even with the right password"

    disabled, _ = make_app(tmp_path / "d", password="")
    session = disabled.get("/webui/api/session").json()
    assert session["enabled"] is False and "WEBUI_PASSWORD" in session["reason"]
    assert disabled.post("/webui/api/login", json={"password": ""}, headers=HEAD).status_code == 503
