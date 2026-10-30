"""工具接口的 HTTP 层。不依赖 NoneBot，方便单独测试。

安全措施：
- 只接受来自本机（127.0.0.1 / ::1）或 Docker 内网的请求；公网请求一律 403。
- 必须带 Authorization: Bearer <TOOLS_API_TOKEN>，用常量时间比较。
- 参数在工具函数里逐个校验，不信任调用方。
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .._shared.status import event_log
from .tools import ToolContext, ToolError, ToolSpec


def _brief(args: dict[str, Any]) -> str:
    """给运行状态页看的参数摘要。recent_chat 是群聊内容，不记。"""
    parts = []
    for key, value in args.items():
        if key == "recent_chat":
            continue
        text = str(value)
        parts.append(f"{key}={text[:40]}{'…' if len(text) > 40 else ''}")
    return "，".join(parts)


# 只放行本机和局域网 / Docker 网桥。不用 ip.is_private：它把 203.0.113.0/24 这类文档地址也算进去。
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def is_local(host: str | None) -> bool:
    """本机或私有网段（MaiBot 跑在宿主机，NoneBot 在 Docker 里时，请求来自 docker 网桥）。"""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOCAL_NETWORKS)


def create_router(
    tools: dict[str, ToolSpec],
    token: Callable[[], str],
    *,
    prefix: str = "/tools",
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    def authorized(request: Request) -> JSONResponse | None:
        if not is_local(request.client.host if request.client else None):
            return error(403, "只允许本机调用")
        expected = token()
        if not expected:
            return error(503, "工具接口未启用：没有设置 TOOLS_API_TOKEN")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return error(401, "令牌不正确")
        return None

    @router.get("", include_in_schema=False)
    async def list_tools(request: Request) -> JSONResponse:
        if denied := authorized(request):
            return denied
        return JSONResponse({"ok": True, "tools": [spec.describe() for spec in tools.values()]})

    @router.post("/{name}", include_in_schema=False)
    async def call_tool(name: str, request: Request) -> JSONResponse:
        if denied := authorized(request):
            return denied
        spec = tools.get(name)
        if spec is None:
            return error(404, f"没有这个工具：{name}")
        try:
            body: Any = await request.json()
        except ValueError:
            return error(400, "请求格式不正确")
        if not isinstance(body, dict):
            return error(400, "请求格式不正确")
        who = str(body.get("user_id") or "?")[:12]
        where = f"群 {str(body.get('group_id'))[:12]}" if body.get("group_id") else "私聊"
        try:
            ctx = ToolContext.from_body(body)
            args = body.get("args") or {}
            if not isinstance(args, dict):
                raise ToolError("args 必须是对象")
            result = await spec.run(ctx, args)
        except ToolError as e:
            event_log.record("tools", "warn", name, f"{who}（{where}）没做成：{e}")
            return JSONResponse({"ok": False, "message": str(e)})
        event_log.record("tools", "ok", name, f"{who}（{where}）{_brief(args)}")
        return JSONResponse({"ok": True, **result})

    return router
