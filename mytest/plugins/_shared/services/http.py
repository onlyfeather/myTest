from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx


class ServiceError(Exception):
    """调用外部服务失败。message 是可以直接发给用户看的中文说明。"""


@asynccontextmanager
async def http_client(proxy: str = "", timeout: float = 60.0) -> AsyncIterator[httpx.AsyncClient]:
    # 每次请求按当前配置新建客户端，这样在 WebUI 改了代理或超时立即生效。
    async with httpx.AsyncClient(
        proxy=proxy.strip() or None,
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=True,
    ) as client:
        yield client


def describe_http_error(name: str, resp: httpx.Response) -> str:
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
            elif err:
                detail = str(err)
            detail = detail or str(data.get("message") or data.get("detail") or "")
    except ValueError:
        detail = resp.text
    hints = {
        401: "密钥无效或已过期",
        402: "余额或点数不足",
        403: "没有权限（检查密钥、订阅或地区限制）",
        404: "接口地址或模型不存在",
        429: "请求太频繁或额度用完了",
    }
    hint = hints.get(resp.status_code, "")
    parts = [f"{name} 返回 HTTP {resp.status_code}"]
    if hint:
        parts.append(hint)
    if detail:
        parts.append(detail.strip()[:200])
    return "：".join(parts[:1]) + ("，" + "，".join(parts[1:]) if len(parts) > 1 else "")


def _target(exc: Exception) -> str:
    try:
        url = exc.request.url  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        return ""
    return f"{url.scheme}://{url.host}" + (f":{url.port}" if url.port else "")


def _detail(exc: Exception) -> str:
    text = str(exc).strip()
    # 密钥不会出现在异常信息里，但 URL 可能很长，截断一下。
    return text[:160] if text else type(exc).__name__


def describe_exception(name: str, exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return str(exc)
    target = _target(exc)
    where = f"（{target}）" if target else ""
    if isinstance(exc, httpx.ProxyError):
        return f"{name} 代理连接失败{where}：{_detail(exc)}。检查代理地址和端口是否正确、代理是否在运行"
    if isinstance(exc, httpx.ConnectTimeout):
        return f"{name} 连接超时{where}：服务器连不上接口地址，国内服务器一般需要填代理"
    if isinstance(exc, httpx.TimeoutException):
        return f"{name} 请求超时{where}：已经连上但等太久没有返回，可以调大超时时间"
    if isinstance(exc, httpx.ConnectError):
        detail = _detail(exc)
        hint = "域名解析失败，检查接口地址拼写" if "Name or service" in detail or "getaddrinfo" in detail \
            else "服务器访问不到接口地址，国内服务器一般需要填代理"
        return f"{name} 连接失败{where}：{hint}（{detail}）"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return f"{name} 接口地址格式不对，需要以 http:// 或 https:// 开头"
    return f"{name} 出错：{type(exc).__name__}：{_detail(exc)}"
