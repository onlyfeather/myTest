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


def describe_exception(name: str, exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return f"{name} 请求超时"
    if isinstance(exc, httpx.ProxyError):
        return f"{name} 代理连接失败，检查代理地址"
    if isinstance(exc, httpx.ConnectError):
        return f"{name} 连接失败（服务器访问不到接口地址，可能需要代理）"
    return f"{name} 出错：{type(exc).__name__}"
