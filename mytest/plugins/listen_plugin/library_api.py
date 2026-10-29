"""一起听页面上的个人曲库接口：喜欢的音乐、导入歌单、扫码登录、解绑。

凭证从不返回给浏览器：扫码会话只给浏览器一个随机 sid，二维码图片和状态，平台给的登录信息只在服务端。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from .app_shared import RateLimiter
from .auth import Identity
from .bridge import BotUnavailable
from .library import Library, LibraryError
from .service import ListenService
from .sources.base import PLATFORMS, SourceError, Track, parse_ref

Current = Callable[[Request], Awaitable[tuple[Identity, Any] | JSONResponse]]


class ImportBody(BaseModel):
    platform: str = Field(max_length=16)
    via: str = Field(pattern="^(uid|login)$")
    remote_ids: list[str] = Field(max_length=50)


class UidBody(BaseModel):
    platform: str = Field(max_length=16)
    account_id: str = Field(min_length=1, max_length=20)


class QrBody(BaseModel):
    platform: str = Field(max_length=16)
    kind: str = Field(default="", max_length=8)


class LikeBody(BaseModel):
    ref: str = Field(max_length=80)


class HideBody(BaseModel):
    hidden: bool


def register(app: FastAPI, service: ListenService, library: Library, *, current: Current,
             same_origin: Callable[[Request], bool]) -> None:
    qr_limiter = RateLimiter(10, 300)
    import_limiter = RateLimiter(20, 600)

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    async def body_of(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
        if not same_origin(request):
            return error(403, "来源不对")
        try:
            return model.model_validate(await request.json())
        except (ValueError, ValidationError):
            return error(400, "请求格式不对")

    def platform_ok(platform: str) -> bool:
        return platform in PLATFORMS and platform in library.accounts

    # ------------------------------------------------------------ 我的账号、扫码

    @app.get("/api/library/accounts")
    async def my_accounts(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        return JSONResponse({
            "ok": True,
            "enabled": library.vault is not None,
            "platforms": library.platforms(),
            "accounts": library.credential_status(identity.qq),
        })

    @app.post("/api/library/qr")
    async def qr_start(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        body = await body_of(request, QrBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, QrBody)
        if not platform_ok(body.platform):
            return error(400, "这个平台没有接入")
        if not qr_limiter.allow(identity.qq):
            return error(429, "二维码要得太频繁了，过几分钟再试")
        try:
            sid, start = await library.qr_start(identity.qq, body.platform, body.kind)
        except LibraryError as e:
            return error(502, str(e))
        return JSONResponse({"ok": True, "sid": sid, "image": start.image, "hint": start.hint})

    @app.get("/api/library/qr/{sid}")
    async def qr_poll(sid: str, request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        if len(sid) > 64:
            return error(404, "二维码过期了")
        try:
            result = await library.qr_poll(identity.qq, sid)
        except LibraryError as e:
            return error(502, str(e))
        return JSONResponse({"ok": True, **result})

    @app.post("/api/library/accounts/{platform}/logout")
    async def unbind(platform: str, request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        if not same_origin(request):
            return error(403, "来源不对")
        if not platform_ok(platform):
            return error(404, "没有这个平台")
        library.logout(found[0].qq, platform)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ 导入

    @app.post("/api/library/uid")
    async def list_uid(request: Request) -> Response:
        """填用户 ID：列出这个账号的公开歌单，让用户勾选。"""
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        body = await body_of(request, UidBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, UidBody)
        if not platform_ok(body.platform):
            return error(400, "这个平台没有接入")
        if not import_limiter.allow(found[0].qq):
            return error(429, "操作太频繁了，过一会儿再试")
        try:
            playlists = await library.list_by_uid(found[0].qq, body.platform, body.account_id)
        except LibraryError as e:
            return error(502, str(e))
        return JSONResponse({"ok": True, "playlists": playlists})

    @app.post("/api/library/import")
    async def import_playlists(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        body = await body_of(request, ImportBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, ImportBody)
        if not platform_ok(body.platform):
            return error(400, "这个平台没有接入")
        if not import_limiter.allow(found[0].qq):
            return error(429, "操作太频繁了，过一会儿再试")
        try:
            imported = await library.import_playlists(found[0].qq, body.platform, body.remote_ids, via=body.via)
        except LibraryError as e:
            return error(502, str(e))
        return JSONResponse({"ok": True, "imported": imported})

    # ------------------------------------------------------------ 查看

    @app.get("/api/library/{qq}")
    async def overview(qq: str, request: Request) -> Response:
        """某人的“喜欢的音乐”和歌单。只给同群成员看；别人看不到他设成仅自己可见的歌单。"""
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        if not qq.isdigit() or len(qq) > 12:
            return error(404, "没有这个人")
        if qq != identity.qq:
            try:
                if await service.members.member(identity.group_id, qq) is None:
                    return error(404, "没有这个人")
            except BotUnavailable:
                return error(503, "凛音暂时不在线")
        data = library.overview(qq, viewer=identity.qq)
        tracks = [Track.from_dict(t) for t in data["favorites"]["tracks"]]
        service.remember(tracks)  # 资料卡里的歌可以直接点
        return JSONResponse({"ok": True, "mine": qq == identity.qq, **data})

    @app.get("/api/playlists/{playlist_id}")
    async def playlist(playlist_id: int, request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        identity, _ = found
        data = library.playlist(playlist_id, viewer=identity.qq)
        if data is None:
            return error(404, "没有这个歌单，或者主人设成了仅自己可见")
        if data["qq"] != identity.qq:
            try:
                if await service.members.member(identity.group_id, data["qq"]) is None:
                    return error(404, "没有这个歌单")
            except BotUnavailable:
                return error(503, "凛音暂时不在线")
        tracks = []
        for raw in data["tracks"]:
            try:
                tracks.append(Track.from_dict(raw))
            except SourceError:
                continue
        service.remember(tracks)
        return JSONResponse({"ok": True, **{k: v for k, v in data.items() if k != "tracks"},
                             "mine": data["qq"] == identity.qq, "tracks": [t.to_dict() for t in tracks]})

    @app.post("/api/playlists/{playlist_id}/visibility")
    async def set_hidden(playlist_id: int, request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        body = await body_of(request, HideBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, HideBody)
        if not library.store.set_playlist_hidden(found[0].qq, playlist_id, body.hidden):
            return error(404, "没有这个歌单")
        return JSONResponse({"ok": True})

    @app.post("/api/playlists/{playlist_id}/delete")
    async def delete_playlist(playlist_id: int, request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        if not same_origin(request):
            return error(403, "来源不对")
        if not library.store.delete_playlist(found[0].qq, playlist_id):
            return error(404, "没有这个歌单")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ 喜欢的音乐

    @app.post("/api/favorites")
    async def like(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        body = await body_of(request, LikeBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, LikeBody)
        try:
            parse_ref(body.ref)
        except SourceError:
            return error(400, "歌曲编号不对")
        track = service.known_track(body.ref)
        if track is None:
            room = service.hub.get(found[0].group_id)
            items = ([room.current] if room and room.current else []) + (room.queue if room else [])
            track = next((i.track for i in items if i.track.ref == body.ref), None)
        if track is None:
            return error(404, "找不到这首歌，重新搜一下")
        try:
            added = library.like(found[0].qq, track)
        except LibraryError as e:
            return error(400, str(e))
        return JSONResponse({"ok": True, "added": added})

    @app.post("/api/favorites/remove")
    async def unlike(request: Request) -> Response:
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        body = await body_of(request, LikeBody)
        if isinstance(body, JSONResponse):
            return body
        assert isinstance(body, LikeBody)
        library.unlike(found[0].qq, body.ref)
        return JSONResponse({"ok": True})

    @app.get("/api/favorites/refs")
    async def liked_refs(request: Request) -> Response:
        """我喜欢过哪些歌（页面上给 ♥ 上色用）。"""
        found = await current(request)
        if isinstance(found, JSONResponse):
            return found
        return JSONResponse({"ok": True, "refs": sorted(library.liked_refs(found[0].qq))})
