"""一起听的 FastAPI 应用：页面、登录、搜索、音频地址、WebSocket。不 import nonebot。

安全措施：
- 只有白名单群的成员能登录；每次建立 WebSocket 都重新检查成员资格（结果缓存 10 分钟）。
- 会话 cookie：HttpOnly、SameSite=Lax、HTTPS 时 Secure，只在一起听的域名下。
- 改状态的请求（POST 和 WebSocket）检查 Origin，防止别的网站借用户的 cookie 操作。
- 只采信 trusted_proxies 传来的 X-Forwarded-For。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .auth import SESSION_TTL_MS, AuthError, Identity
from .bridge import BotUnavailable
from .hub import Connection
from .room import RoomError
from .service import ListenService
from .sources.base import PLATFORM_NAMES, SourceError, Track

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "listen_session"
BROWSER_COOKIE = "listen_browser"
MAX_WS_MESSAGE = 4096
WS_RATE = 5  # 每个连接每秒最多几条消息
MAX_CHAT = 200

def security_headers_for(origin: str) -> dict[str, str]:
    ws_origin = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        # 不用构建工具，Vue 在浏览器里编译模板要用 new Function，所以需要 unsafe-eval。
        # 模板只来自 static/ 里的文件，用户内容一律走文本插值，不会进入模板。
        "Content-Security-Policy": (
            f"default-src 'self'; img-src 'self' https: data:; media-src https: blob:; "
            f"connect-src 'self' {ws_origin}; style-src 'self'; script-src 'self' 'unsafe-eval'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        ),
    }


# ---------------------------------------------------------------- 消息格式


class ControlMsg(BaseModel):
    type: Literal["control"]
    action: Literal["play", "pause", "seek", "next"]
    position_ms: int | None = Field(default=None, ge=0, le=24 * 3600 * 1000)
    version: int | None = None


class QueueMsg(BaseModel):
    type: Literal["queue"]
    op: Literal["add", "remove", "move"]
    ref: str | None = Field(default=None, max_length=80)
    item_id: int | None = None
    to: int | None = Field(default=None, ge=0, le=1000)


class ChatMsg(BaseModel):
    type: Literal["chat"]
    text: str = Field(min_length=1, max_length=MAX_CHAT)


class PingMsg(BaseModel):
    type: Literal["ping"]
    client_ms: int


class SettingMsg(BaseModel):
    type: Literal["setting"]
    allow_control: bool


_MESSAGES: dict[str, type[BaseModel]] = {
    "control": ControlMsg,
    "queue": QueueMsg,
    "chat": ChatMsg,
    "ping": PingMsg,
    "setting": SettingMsg,
}


def parse_message(raw: str) -> BaseModel:
    if len(raw) > MAX_WS_MESSAGE:
        raise ValueError("消息太长")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("消息格式不对")
    model = _MESSAGES.get(str(data.get("type")))
    if model is None:
        raise ValueError("未知的消息类型")
    return model.model_validate(data)


# ---------------------------------------------------------------- 工具函数


def client_ip(peer: str | None, forwarded: str | None, trusted: tuple[str, ...]) -> str:
    """只有直接连过来的是受信任的反代时，才用 X-Forwarded-For 里最后一个不受信任的地址。"""
    if not peer:
        return ""
    if peer not in trusted or not forwarded:
        return peer
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue
        if hop not in trusted:
            return hop
    return peer


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True


def create_app(service: ListenService) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    config = service.config
    login_limiter = RateLimiter(20, 60)
    search_limiter = RateLimiter(30, 60)
    fallback_locks: dict[str, asyncio.Lock] = {}
    headers = security_headers_for(config.origin)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        for key, value in headers.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def ip_of(request: Request | WebSocket) -> str:
        return client_ip(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            config.trusted_proxies,
        )

    def same_origin(request: Request | WebSocket) -> bool:
        return request.headers.get("origin", "") == config.origin

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    def set_session(resp: Response, token: str) -> None:
        resp.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_TTL_MS // 1000, httponly=True,
            secure=config.secure, samesite="lax", path="/",
        )

    async def identity_of(request: Request | WebSocket) -> Identity | None:
        return service.sessions.get(request.cookies.get(SESSION_COOKIE), service.clock())

    async def current(request: Request) -> tuple[Identity, Any] | JSONResponse:
        identity = await identity_of(request)
        if identity is None:
            return error(401, "请先登录")
        try:
            member = await service.check_member(identity)
        except BotUnavailable:
            return error(503, "凛音暂时不在线，稍后再试")
        if member is None:
            service.sessions.revoke(request.cookies.get(SESSION_COOKIE))
            return error(403, "你不在这个群里，或者这个群没有开放一起听")
        return identity, member

    # ------------------------------------------------------------ 页面

    @app.get("/", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------ 登录

    @app.post("/api/login/link")
    async def login_link(request: Request) -> Response:
        if not same_origin(request):
            return error(403, "来源不对")
        if not login_limiter.allow(ip_of(request)):
            return error(429, "试得太频繁了，稍后再试")
        try:
            body = await request.json()
            token = str(body.get("token") or "")[:600]
        except (ValueError, AttributeError):
            return error(400, "请求格式不对")
        try:
            session, member = await service.login_with_link(token)
        except AuthError as e:
            return error(400, str(e))
        except BotUnavailable:
            return error(503, "凛音暂时不在线，稍后再试")
        resp = JSONResponse({"ok": True, "name": member.name})
        set_session(resp, session)
        return resp

    @app.post("/api/login/code")
    async def login_code(request: Request) -> Response:
        """方案 B：拿一个验证码，或者查验证码有没有被兑换。"""
        if not same_origin(request):
            return error(403, "来源不对")
        browser = request.cookies.get(BROWSER_COOKIE) or ""
        new_browser = not (16 <= len(browser) <= 64)
        if new_browser:
            browser = secrets.token_urlsafe(24)
        now = service.clock()
        identity = service.codes.poll(browser, now)
        if identity is not None:
            try:
                member = await service.check_member(identity)
            except BotUnavailable:
                return error(503, "凛音暂时不在线，稍后再试")
            if member is None:
                return error(403, "你不在这个群里，或者这个群没有开放一起听")
            resp: Response = JSONResponse({"ok": True, "done": True, "name": member.name})
            set_session(resp, service.sessions.create(identity, now))
            return resp
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        existing = service.codes.current(browser, now)
        if body.get("renew") or existing is None:
            if not login_limiter.allow(ip_of(request)):
                return error(429, "试得太频繁了，稍后再试")
            try:
                service.codes.create(browser, now)
            except AuthError as e:
                return error(429, str(e))
            existing = service.codes.current(browser, now)
        # 每次都把当前有效的码带上：刷新页面、开了几个标签页时，显示的都是同一个能用的码。
        code, expires = existing or ("", now)
        resp = JSONResponse({"ok": True, "done": False, "code": code, "expires_in_ms": max(0, expires - now)})
        if new_browser:
            resp.set_cookie(BROWSER_COOKIE, browser, max_age=600, httponly=True, secure=config.secure,
                            samesite="strict", path="/api/login")
        return resp

    @app.post("/api/logout")
    async def logout(request: Request) -> Response:
        if not same_origin(request):
            return error(403, "来源不对")
        service.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    async def me(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, member = found
        return JSONResponse({
            "ok": True,
            "qq": identity.qq,
            "group_id": identity.group_id,
            "name": member.name,
            "superuser": service.is_superuser(identity.qq),
            "room_open": service.hub.get(identity.group_id) is not None,
        })

    # ------------------------------------------------------------ 头像

    @app.get("/avatar/{qq}")
    async def avatar(qq: str, request: Request) -> Response:
        # 头像地址里带 QQ 号，经服务器转发；只给同群成员看。
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        if not qq.isdigit() or len(qq) > 12:
            return error(404, "没有这个人")
        try:
            if qq != identity.qq and await service.members.member(identity.group_id, qq) is None:
                return error(404, "没有这个人")
        except BotUnavailable:
            return error(503, "凛音暂时不在线")
        data = await fetch_avatar(qq)
        if data is None:
            return error(404, "头像获取失败")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------ 搜索和播放地址

    @app.get("/api/search")
    async def search(request: Request, q: str = "", platform: str = "qq") -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        keyword = q.strip()[:60]
        if not keyword:
            return JSONResponse({"ok": True, "tracks": []})
        if not search_limiter.allow(identity.qq):
            return error(429, "搜得太快了，稍等一下")
        if platform not in service.sources:
            return error(400, f"{PLATFORM_NAMES.get(platform, '这个平台')}没有接入")
        try:
            tracks = await service.search(keyword, platform)
        except SourceError as e:
            return error(502, str(e))
        return JSONResponse({
            "ok": True,
            "tracks": [t.to_dict() for t in tracks],
            "vip_ready": service.vip_ready(platform),
        })

    @app.get("/api/platforms")
    async def platforms(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        return JSONResponse({"ok": True, "platforms": service.platforms()})

    @app.get("/api/lyrics")
    async def lyrics(request: Request, ref: str = "") -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        track = service.known_track(ref)
        if track is None:
            room = service.hub.get(found[0].group_id)
            items = ([room.current] if room and room.current else []) + (room.queue if room else [])
            track = next((i.track for i in items if i.track.ref == ref), None)
        if track is None:
            return error(404, "找不到这首歌")
        try:
            lrc, trans = await service.lyrics(track)
        except SourceError as e:
            return error(502, str(e))
        return JSONResponse({"ok": True, "lrc": lrc[:200_000], "trans": trans[:200_000]})

    @app.get("/api/members/{qq}")
    async def member_profile(qq: str, request: Request) -> Response:
        """资料卡。只给同群成员看。"""
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        if not qq.isdigit() or len(qq) > 12:
            return error(404, "没有这个人")
        try:
            member = await service.members.member(identity.group_id, qq)
            profile = await service.profile(identity.group_id, qq) if member else None
        except BotUnavailable:
            return error(503, "凛音暂时不在线")
        if member is None or profile is None:
            return error(404, "没有这个人")
        return JSONResponse({"ok": True, **profile})

    @app.get("/api/stream")
    async def stream(request: Request, ref: str = "") -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        room = service.hub.get(identity.group_id)
        # 只给本群房间里正在放和即将放的歌取地址。
        items = ([room.current] if room and room.current else []) + (room.queue[:2] if room else [])
        track = next((i.track for i in items if i.track.ref == ref), None)
        if track is None:
            return error(404, "这首歌不在播放队列里")
        try:
            result = await service.stream(track, service.bitrate_for(identity.group_id))
        except SourceError as e:
            await handle_unplayable(identity.group_id, track, str(e))
            return error(502, str(e))
        return JSONResponse({"ok": True, "url": result.url, "bitrate": result.bitrate})

    async def handle_unplayable(group_id: str, track: Track, reason: str) -> None:
        """正在放的这首拿不到地址：先在别的平台找同一首换上，找不到再跳过，不让房间卡住。

        多个听众会同时报错，用锁保证只处理一次。
        """
        lock = fallback_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            room = service.hub.get(group_id)
            if room is None or room.current is None or room.current.track.ref != track.ref:
                return
            label = track.label()
            replacement = await service.substitute(track)
            room = service.hub.get(group_id)
            if room is None or room.current is None or room.current.track.ref != track.ref:
                return
            if replacement is not None:
                room.replace_current(replacement, service.clock())
                note = f"{label} 在{PLATFORM_NAMES[track.platform]}放不了，换成{PLATFORM_NAMES[replacement.platform]}的同一首"
            else:
                room.skip_current(service.clock())
                note = f"{label} 放不了，跳过了（{reason}）"
            service.hub.save(group_id)
            await service.hub.push_queue(group_id)
            await service.hub.push_state(group_id)
            await system_chat(group_id, note)

    # ------------------------------------------------------------ WebSocket

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if not same_origin(socket):
            await socket.close(4403, "bad origin")
            return
        identity = await identity_of(socket)
        if identity is None:
            await socket.close(4401, "login required")
            return
        try:
            member = await service.check_member(identity)
        except BotUnavailable:
            await socket.close(4503, "bot offline")
            return
        if member is None:
            await socket.close(4403, "not a member")
            return
        group_id = identity.group_id
        room = service.hub.get(group_id)
        if room is None:
            await socket.accept()
            await socket.send_json({"type": "closed", "message": "现在没有一起听，在群里发 /一起听 开一个"})
            await socket.close(4004, "no room")
            return
        if service.hub.online(group_id) >= config.max_members() and not any(
            m["qq"] == identity.qq for m in service.hub.members(group_id)
        ):
            await socket.accept()
            await socket.send_json({"type": "closed", "message": f"房间满了（最多 {config.max_members()} 人）"})
            await socket.close(4029, "full")
            return

        await socket.accept()
        superuser = service.is_superuser(identity.qq)
        lock = asyncio.Lock()

        async def send(message: dict[str, Any]) -> None:
            async with lock:
                await socket.send_json(message)

        async def close(code: int, reason: str) -> None:
            await socket.close(code, reason)

        conn = Connection(member=member, superuser=superuser, send=send, close=close)
        service.hub.join(group_id, conn)
        hits: deque[float] = deque()
        throttled = False
        try:
            now = service.clock()
            await send({"type": "hello", "qq": identity.qq, "name": member.name, "superuser": superuser})
            await send({"type": "state", "state": room.state(now)})
            await send({"type": "queue", "queue": room.queue_dict()})
            await service.hub.broadcast(group_id, {"type": "members", "members": service.hub.members(group_id)})
            while True:
                raw = await socket.receive_text()
                t = time.monotonic()
                while hits and t - hits[0] >= 1:
                    hits.popleft()
                if len(hits) >= WS_RATE:
                    if not throttled:
                        throttled = True
                        await send({"type": "error", "message": "操作太快了，慢一点"})
                    continue
                throttled = False
                hits.append(t)
                try:
                    msg = parse_message(raw)
                except (ValueError, ValidationError):
                    await send({"type": "error", "message": "消息格式不对"})
                    continue
                await handle(group_id, conn, msg)
        except WebSocketDisconnect:
            pass
        finally:
            service.hub.leave(group_id, conn)
            await service.hub.broadcast(group_id, {"type": "members", "members": service.hub.members(group_id)})

    async def system_chat(group_id: str, text: str) -> None:
        await service.hub.broadcast(
            group_id, {"type": "chat", "system": True, "text": text, "ts": service.clock()}
        )

    async def handle(group_id: str, conn: Connection, msg: BaseModel) -> None:
        room = service.hub.get(group_id)
        if room is None:
            return
        qq, name, su = conn.member.qq, conn.member.name, conn.superuser
        now = service.clock()
        try:
            if isinstance(msg, PingMsg):
                await conn.send({"type": "pong", "client_ms": msg.client_ms, "server_ms": service.clock()})
            elif isinstance(msg, ChatMsg):
                text = msg.text.strip()
                if text:
                    await service.hub.broadcast(group_id, {
                        "type": "chat", "from": qq, "name": name, "text": text, "ts": now,
                    })
            elif isinstance(msg, ControlMsg):
                before = room.current.track.label() if room.current else ""
                if msg.action == "play":
                    room.play(qq, now, version=msg.version, superuser=su)
                    after = room.current.track.label() if room.current else ""
                    note = f"{name} 继续播放" if before else f"{name} 开始播放：{after}"
                elif msg.action == "pause":
                    room.pause(qq, now, version=msg.version, superuser=su)
                    note = f"{name} 暂停了"
                elif msg.action == "seek":
                    room.seek(qq, now, msg.position_ms or 0, version=msg.version, superuser=su)
                    note = f"{name} 拖到了 {fmt_time(msg.position_ms or 0)}"
                else:
                    room.next(qq, now, version=msg.version, superuser=su)
                    after = room.current.track.label() if room.current else "（没有歌了）"
                    note = f"{name} 切到了下一首：{after}" if before else f"{name} 开始播放：{after}"
                service.hub.save(group_id)
                await service.hub.push_state(group_id)
                if msg.action in ("next", "play"):
                    await service.hub.push_queue(group_id)
                await system_chat(group_id, note)
            elif isinstance(msg, QueueMsg):
                if msg.op == "add":
                    track = service.known_track(msg.ref or "")
                    if track is None:
                        raise RoomError("找不到这首歌，重新搜一下")
                    note = f"{name} 点了 {track.label()}"
                    if not service.playable(track):
                        # 会员歌在原平台放不了：在别的平台找同一首能放的，找不到就不让点（进了队列会卡住所有人）。
                        replacement = await service.substitute(track)
                        if replacement is None:
                            raise RoomError("这是会员歌，别的平台也没找到能放的同一首")
                        note += f"（{PLATFORM_NAMES[track.platform]}要会员，换成{PLATFORM_NAMES[replacement.platform]}的）"
                        track = replacement
                        room = service.hub.get(group_id)
                        if room is None:
                            return
                        now = service.clock()
                    room.add(track, qq, name)
                    await system_chat(group_id, note)
                    if room.current is None:
                        room.next(qq, now, superuser=True)
                        await service.hub.push_state(group_id)
                elif msg.op == "remove":
                    room.remove(int(msg.item_id or 0), qq, superuser=su)
                else:
                    room.move(int(msg.item_id or 0), int(msg.to or 0), qq, superuser=su)
                service.hub.save(group_id)
                await service.hub.push_queue(group_id)
            elif isinstance(msg, SettingMsg):
                room.set_allow_control(msg.allow_control, qq, superuser=su)
                service.hub.save(group_id)
                await service.hub.push_state(group_id)
                await system_chat(group_id, f"{name} {'允许' if msg.allow_control else '关掉了'}大家控制播放")
        except RoomError as e:
            await conn.send({"type": "error", "message": str(e)})
            latest = service.hub.get(group_id)  # 换源时房间可能已经关了
            if latest is not None:
                await conn.send({"type": "state", "state": latest.state(service.clock())})

    app.state.handle_unplayable = handle_unplayable  # 测试并发换源时直接调用
    return app


def fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


_avatars: dict[str, tuple[float, bytes]] = {}


async def fetch_avatar(qq: str) -> bytes | None:
    import httpx

    hit = _avatars.get(qq)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://q1.qlogo.cn/g", params={"b": "qq", "nk": qq, "s": "100"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return None
    if len(_avatars) > 500:
        _avatars.clear()
    _avatars[qq] = (time.monotonic() + 3600, resp.content)
    return resp.content
