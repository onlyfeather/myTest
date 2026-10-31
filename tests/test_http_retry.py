"""连接失败自动重试一次。运行：python -m pytest tests/test_http_retry.py -q"""

import asyncio

import httpx
import pytest

from mytest.plugins._shared.services.http import RetryingClient


def client(handler, retries=1):
    return RetryingClient(transport=httpx.MockTransport(handler), retries=retries, retry_delay=0)


def flaky(*errors):
    """依次抛出这些错误，之后返回 200，并记下每次收到的请求体。"""
    queue = list(errors)
    bodies = []

    def handler(request):
        bodies.append(request.read())
        if queue:
            raise queue.pop(0)("boom", request=request)
        return httpx.Response(200, text="ok")

    return handler, bodies


async def post(c, **kwargs):
    async with c:
        return await c.post("https://api.example/v1/images/edits", **kwargs)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError])
def test_retries_connect_errors_once(error):
    handler, bodies = flaky(error)
    r = asyncio.run(post(client(handler), json={"prompt": "猫"}))
    assert r.status_code == 200 and len(bodies) == 2 and bodies[0] == bodies[1]


def test_multipart_upload_is_resent_complete():
    """带参考图的请求（multipart）重试时，第二次发出的请求体和第一次完全一样。"""
    handler, bodies = flaky(httpx.ConnectError)
    image = b"\x89PNG" + bytes(range(256)) * 400
    r = asyncio.run(post(client(handler), data={"model": "m", "prompt": "猫"},
                         files=[("image[]", ("reference_1.png", image, "image/png"))]))
    assert r.status_code == 200 and len(bodies) == 2
    assert bodies[0] == bodies[1] and image in bodies[1]


def test_gives_up_after_retries():
    handler, bodies = flaky(httpx.ConnectError, httpx.ConnectError)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(post(client(handler), json={}))
    assert len(bodies) == 2


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_does_not_retry_after_request_may_have_arrived(error):
    """读超时、对方中途断开：请求可能已经到了，再发一次可能多扣一次钱。"""
    handler, bodies = flaky(error)
    with pytest.raises(error):
        asyncio.run(post(client(handler), json={}))
    assert len(bodies) == 1


def test_retries_zero_means_no_retry():
    handler, bodies = flaky(httpx.ConnectError)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(post(client(handler, retries=0), json={}))
    assert len(bodies) == 1


def test_only_replayable_bodies_are_resent():
    """只能读一次的请求体（生成器）不能重放。MockTransport 会先把请求体读进内存，所以这里直接检查判断函数。"""
    from mytest.plugins._shared.services.http import _replayable

    async def gen():
        yield b"part"

    assert _replayable(httpx.Request("POST", "https://a", json={"x": 1}))
    assert _replayable(httpx.Request("POST", "https://a", data={"k": "v"},
                                     files=[("image[]", ("a.png", b"png", "image/png"))]))
    assert not _replayable(httpx.Request("POST", "https://a", content=gen()))


def test_http_client_still_reads_env_proxy(monkeypatch):
    """没填代理时和以前一样，会读环境变量里的代理。"""
    from mytest.plugins._shared.services.http import http_client

    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:7890")

    async def mounts():
        async with http_client("") as c:
            return [str(p.pattern) for p in c._mounts]

    assert any("https" in m for m in asyncio.run(mounts()))
