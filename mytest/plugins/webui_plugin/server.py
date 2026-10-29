"""WebUI 的 HTTP 接口。不依赖 NoneBot，方便单独测试。

安全措施（WebUI 会暴露在公网）：
- 密码只能在 .env 里设置（WEBUI_PASSWORD），WebUI 自己改不了；没设置就不开放。
- 同一 IP 连续输错 5 次锁 15 分钟，全局每分钟最多 30 次失败尝试。
- 会话令牌是随机值，存在内存里，HttpOnly + SameSite=Strict，12 小时过期。
- 修改类请求必须带 X-Requested-With 头，防止跨站提交。
- 密钥类配置永远不会返回给浏览器。
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .._shared.services.http import describe_exception
from .._shared.settings import SettingsStore

COOKIE_NAME = "mytest_webui"
SESSION_TTL = 12 * 3600
MAX_FAILS_PER_IP = 5
LOCK_SECONDS = 15 * 60
MAX_FAILS_PER_MINUTE = 30
INDEX_HTML = Path(__file__).with_name("index.html")


class LoginGuard:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._recent: deque[float] = deque()

    def _prune(self, ip: str) -> list[float]:
        now = self._clock()
        fails = [t for t in self._fails.get(ip, []) if now - t < LOCK_SECONDS]
        self._fails[ip] = fails
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        return fails

    def blocked_for(self, ip: str) -> int:
        fails = self._prune(ip)
        if len(fails) >= MAX_FAILS_PER_IP:
            return int(fails[0] + LOCK_SECONDS - self._clock()) + 1
        if len(self._recent) >= MAX_FAILS_PER_MINUTE:
            return 60
        return 0

    def fail(self, ip: str) -> None:
        now = self._clock()
        self._fails.setdefault(ip, []).append(now)
        self._recent.append(now)

    def succeed(self, ip: str) -> None:
        self._fails.pop(ip, None)


class Sessions:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = self._clock() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._tokens.get(token)
        if expires is None:
            return False
        if expires < self._clock():
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)

    def revoke_all(self) -> None:
        self._tokens.clear()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_router(
    store: SettingsStore,
    password: Callable[[], str],
    *,
    prefix: str = "/webui",
    secure_cookie: Callable[[], bool] = lambda: False,
    disabled_reason: Callable[[], str] | None = None,
    guard: LoginGuard | None = None,
    sessions: Sessions | None = None,
) -> APIRouter:
    def reason() -> str:
        if password():
            return ""
        text = disabled_reason() if disabled_reason else ""
        return text or "WebUI 未启用：请先在 .env 里设置 WEBUI_PASSWORD"

    guard = guard or LoginGuard()
    sessions = sessions or Sessions()
    router = APIRouter(prefix=prefix)

    def error(status: int, message: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"ok": False, "message": message, **extra}, status_code=status)

    def authed(request: Request) -> bool:
        return sessions.valid(request.cookies.get(COOKIE_NAME))

    def same_origin_write(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "mytest-webui"

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            INDEX_HTML.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'self' data:; media-src https:; form-action 'self'; frame-ancestors 'none'"
                ),
            },
        )

    @router.get("/api/session", include_in_schema=False)
    async def session(request: Request) -> JSONResponse:
        return JSONResponse(
            {"ok": True, "logged_in": authed(request), "enabled": bool(password()), "reason": reason()}
        )

    @router.post("/api/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        expected = password()
        if not expected:
            return error(503, reason())
        ip = _client_ip(request)
        wait = guard.blocked_for(ip)
        if wait:
            return error(429, f"尝试次数太多，请 {max(1, wait // 60)} 分钟后再试")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        given = str(body.get("password") or "") if isinstance(body, dict) else ""
        if not hmac.compare_digest(given.encode(), expected.encode()):
            guard.fail(ip)
            return error(401, "密码不正确")
        guard.succeed(ip)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME,
            sessions.create(),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="strict",
            secure=secure_cookie(),
            path=prefix,
        )
        return resp

    @router.post("/api/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path=prefix)
        return resp

    @router.get("/api/settings", include_in_schema=False)
    async def get_settings(request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        return JSONResponse(
            {"ok": True, "sections": store.describe(), "pickers": store.describe_pickers()},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/pickers/{picker_id}/search", include_in_schema=False)
    async def search_picker(picker_id: str, request: Request) -> JSONResponse:
        # 和获取列表一样用 POST：会用保存的密钥去请求外部接口。
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            picker = store.picker(picker_id)
        except KeyError:
            return error(404, "没有这个选择页")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            return error(400, "请求格式不正确")
        try:
            page = max(1, min(int(body.get("page") or 1), 1000))
        except (TypeError, ValueError):
            return error(400, "页码不正确")
        title = next((s.title for s in store.sections() if s.id == picker.section_id), picker.title)
        try:
            result = await store.search_picker(
                picker_id,
                query=str(body.get("query") or "")[:100],
                page=page,
                mine=bool(body.get("mine")),
                language=str(body.get("language") or "")[:10],
            )
        except Exception as e:
            return JSONResponse({"ok": False, "message": describe_exception(title, e)})
        return JSONResponse({"ok": True, **result})

    @router.put("/api/settings/{section_id}", include_in_schema=False)
    async def put_settings(section_id: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            body = await request.json()
        except ValueError:
            return error(400, "请求格式不正确")
        if not isinstance(body, dict):
            return error(400, "请求格式不正确")
        values = body.get("values") or {}
        reset = body.get("reset") or []
        if not isinstance(values, dict) or not isinstance(reset, list):
            return error(400, "请求格式不正确")
        try:
            errors = store.update(section_id, values, [str(k) for k in reset])
        except KeyError:
            return error(404, "没有这个配置分区")
        if errors:
            return error(422, "有配置项不合法", errors=errors)
        section = next(s for s in store.describe() if s["id"] == section_id)
        return JSONResponse({"ok": True, "section": section})

    @router.post("/api/settings/{section_id}/options/{key}", include_in_schema=False)
    async def fetch_options(section_id: str, key: str, request: Request) -> JSONResponse:
        # 用 POST：会用保存的密钥去请求外部接口，不应该被随便触发。
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        title = next((s.title for s in store.sections() if s.id == section_id), section_id)
        try:
            options = await store.fetch_options(section_id, key)
        except KeyError:
            return error(404, "这个配置项不支持获取列表")
        except Exception as e:
            return JSONResponse({"ok": False, "message": describe_exception(title, e)})
        return JSONResponse({"ok": True, "options": options})

    @router.get("/api/files/{section_id}/{key}", include_in_schema=False)
    async def get_file(section_id: str, key: str, request: Request) -> Response:
        if not authed(request):
            return error(401, "请先登录")
        try:
            path = store.file_slot(section_id, key).path()
        except KeyError:
            return error(404, "没有这个文件")
        if not path.exists():
            return error(404, "还没有上传")
        return Response(
            path.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.put("/api/files/{section_id}/{key}", include_in_schema=False)
    async def put_file(section_id: str, key: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            slot = store.file_slot(section_id, key)
        except KeyError:
            return error(404, "没有这个文件")
        declared = int(request.headers.get("content-length") or 0)
        if declared > slot.max_bytes:
            return error(413, f"文件太大了，最多 {slot.max_bytes // 1024 // 1024} MB")
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > slot.max_bytes:
                return error(413, f"文件太大了，最多 {slot.max_bytes // 1024 // 1024} MB")
        if not data:
            return error(400, "没有收到文件")
        try:
            message = slot.save(data)
        except Exception as e:
            return error(422, str(e) or "文件不合法")
        return JSONResponse({"ok": True, "message": message})

    @router.delete("/api/files/{section_id}/{key}", include_in_schema=False)
    async def delete_file(section_id: str, key: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            path = store.file_slot(section_id, key).path()
        except KeyError:
            return error(404, "没有这个文件")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return JSONResponse({"ok": True, "message": "已删除" if existed else "本来就没有上传"})

    # ------------------------------------------------------------ 账号面板（扫码登录）

    @router.post("/api/accounts/{section_id}/{platform}/qr", include_in_schema=False)
    async def account_qr_start(section_id: str, platform: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            panel = store.account_panel(section_id)
        except KeyError:
            return error(404, "这个分区没有账号")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        kind = str((body or {}).get("kind") or "")[:8] if isinstance(body, dict) else ""
        try:
            result = await panel.qr_start(platform[:16], kind)
        except Exception as e:
            return JSONResponse({"ok": False, "message": describe_exception(panel.title, e)})
        return JSONResponse({"ok": True, **{k: result.get(k) for k in ("sid", "image", "hint")}},
                            headers={"Cache-Control": "no-store"})

    @router.post("/api/accounts/{section_id}/qr/{sid}", include_in_schema=False)
    async def account_qr_poll(section_id: str, sid: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            panel = store.account_panel(section_id)
        except KeyError:
            return error(404, "这个分区没有账号")
        try:
            result = await panel.qr_poll(sid[:64])
        except Exception as e:
            return JSONResponse({"ok": False, "message": describe_exception(panel.title, e)})
        return JSONResponse({"ok": True, **{k: result.get(k) for k in ("status", "message", "account", "vip")}},
                            headers={"Cache-Control": "no-store"})

    @router.post("/api/accounts/{section_id}/{platform}/logout", include_in_schema=False)
    async def account_logout(section_id: str, platform: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        try:
            store.account_panel(section_id).logout(platform[:16])
        except KeyError:
            return error(404, "没有这个账号")
        return JSONResponse({"ok": True})

    @router.post("/api/settings/{section_id}/test", include_in_schema=False)
    async def test_section(section_id: str, request: Request) -> JSONResponse:
        if not authed(request):
            return error(401, "请先登录")
        if not same_origin_write(request):
            return error(403, "请求来源不正确")
        title = next((s.title for s in store.sections() if s.id == section_id), section_id)
        try:
            message = await store.run_test(section_id)
        except KeyError:
            return error(404, "这个分区不支持测试")
        except Exception as e:
            return JSONResponse({"ok": False, "message": describe_exception(title, e)})
        return JSONResponse({"ok": True, "message": message})

    return router
