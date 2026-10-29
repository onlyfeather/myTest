"""QQ 音乐账号（qqmusic-api-python，进程内调用）。支持 QQ 音乐 App 扫码、手机 QQ 扫码和微信扫码。

凭证是库的 Credential（pydantic 模型）导出成的 dict。二维码会话里的 QR 对象（含图片字节）不能放进 JSON，
所以只在内存里按会话 ID 保存，5 分钟后清掉。

QQ 音乐 App 扫码不是轮询：库通过 MQTT 长连接等服务端推送。这里在后台开一个任务接收推送，
qr_poll 只读任务记下的最新状态。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger

from ..sources.base import Track, https
from ..sources.qqmusic import _cover, _is_vip
from .base import (
    MAX_PLAYLIST_TRACKS,
    AccountError,
    AccountInfo,
    CredentialExpired,
    QrStart,
    QrState,
    RemotePlaylist,
)

QR_TTL = 300
# QQ 音乐“我喜欢”的 dirid
LIKED_DIRID = 201


@dataclass
class _AppScan:
    """一次 QQ 音乐 App 扫码：后台任务写，qr_poll 读。"""

    started: float
    status: str = "waiting"
    credential: dict[str, Any] | None = None
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


def _login_error(e: Exception) -> str:
    """给用户看的登录错误。库的 LoginError 带中文说明（账号受限、设备超限等），其余只报类型。"""
    from qqmusic_api import LoginError

    if isinstance(e, LoginError):
        return f"QQ 音乐登录失败：{str(e)[:80]}"
    return f"QQ 音乐接口出错（{type(e).__name__}）"


_patched = False


def patch_cookie_snapshot() -> None:
    """绕开 qqmusic-api-python 0.8.2 的一个 bug（上游修好后删掉）。

    扫码确认后，QQ 的 check_sig 响应里有值为空的 cookie（例如 pt_recent_uins=）。库的
    core.response.snapshot_payload 用 response.cookies[name] 取值，而 niquests 的 cookie jar 对空值
    会抛 KeyError，于是每次扫码成功都失败。这里换成按 cookie 对象取值，其余和原函数一样。
    """
    global _patched
    if _patched:
        return
    from types import MappingProxyType

    from qqmusic_api.core import executor, response

    def snapshot_payload(resp: Any) -> Any:
        jar = resp.cookies
        try:
            cookies = {c.name: c.value or "" for c in jar}
        except TypeError:  # 不是 cookie jar，是普通映射
            cookies = {str(k): str(v or "") for k, v in dict(jar).items()}
        return response.RawPayload(
            status_code=resp.status_code,
            url=resp.url,
            headers=MappingProxyType(resp.headers),
            cookies=cookies,
            content=resp.content or b"",
            text=resp.text or "",
        )

    response.snapshot_payload = snapshot_payload  # type: ignore[assignment]
    executor.snapshot_payload = snapshot_payload  # type: ignore[attr-defined]
    _patched = True


def _track(song: Any) -> Track | None:
    mid = str(getattr(song, "mid", "") or "")
    if not (10 <= len(mid) <= 20 and mid.isalnum()):
        return None
    album = getattr(song, "album", None)
    album_mid = str(getattr(album, "mid", "") or "")
    file = getattr(song, "file", None)
    media_mid = str(getattr(file, "media_mid", "") or "")
    return Track(
        platform="qq",
        id=mid,
        title=str(getattr(song, "title", "") or getattr(song, "name", "")),
        artists="、".join(str(s.name) for s in getattr(song, "singer", None) or []),
        album=str(getattr(album, "name", "") or ""),
        cover=_cover(album_mid if album_mid.isalnum() else ""),
        duration_ms=int(getattr(song, "interval", 0) or 0) * 1000,
        vip=_is_vip(getattr(song, "pay", None)),
        file_id=media_mid if media_mid.isalnum() else "",
    )


class QQMusicAccounts:
    platform: str = "qq"
    # app：QQ 音乐 App 扫码；qq：手机 QQ 的「扫一扫」；wx：微信扫码
    login_kinds: tuple[str, ...] = ("app", "qq", "wx")

    def __init__(self) -> None:
        self._qr: dict[str, tuple[float, Any]] = {}
        self._app: dict[str, _AppScan] = {}

    def _client(self, credential: dict[str, Any] | None = None) -> Any:
        from qqmusic_api import Client, Credential

        return Client(credential=Credential.model_validate(credential) if credential else None)

    @staticmethod
    def _expired(e: Exception) -> bool:
        from qqmusic_api import (
            CredentialExpiredError,
            CredentialInvalidError,
            LoginAuthExpiredError,
        )

        return isinstance(e, CredentialExpiredError | CredentialInvalidError | LoginAuthExpiredError)

    async def close(self) -> None:
        self._qr.clear()
        for scan in self._app.values():
            scan.cancel()
        self._app.clear()

    # ------------------------------------------------------------ 扫码

    def _purge(self) -> None:
        now = time.monotonic()
        self._qr = {k: v for k, v in self._qr.items() if now - v[0] < QR_TTL}
        for sid in [k for k, v in self._app.items() if now - v.started >= QR_TTL + 30]:
            self._app.pop(sid).cancel()

    async def qr_start(self, kind: str = "app") -> QrStart:
        from qqmusic_api.models.login import QRLoginType

        patch_cookie_snapshot()
        login_type = {"wx": QRLoginType.WX, "qq": QRLoginType.QQ}.get(kind, QRLoginType.MOBILE)
        self._purge()
        try:
            async with self._client() as client:
                qr = await client.login.get_qrcode(login_type)
        except Exception as e:
            logger.opt(exception=e).warning("[一起听] QQ 音乐二维码生成失败")
            raise AccountError(f"QQ 音乐二维码生成失败（{type(e).__name__}）") from e
        sid = secrets.token_urlsafe(16)
        image = f"data:{qr.mimetype or 'image/png'};base64,{base64.b64encode(qr.data).decode()}"
        if login_type == QRLoginType.MOBILE:
            scan = _AppScan(time.monotonic())
            scan.task = asyncio.create_task(self._watch_app(qr, scan), name="listen-qqmusic-app-qr")
            self._app[sid] = scan
            hint = "打开 QQ 音乐 App，用首页的扫一扫扫码"
        else:
            self._qr[sid] = (time.monotonic(), qr)
            hint = "用微信扫码" if kind == "wx" else "用手机 QQ 的扫一扫扫码（QQ 音乐 App 扫不了这个码）"
        return QrStart(session={"sid": sid}, image=image, hint=hint)

    async def _watch_app(self, qr: Any, scan: _AppScan) -> None:
        """后台接收 QQ 音乐 App 扫码的推送，把最新状态记在 scan 上。"""
        import anyio
        from qqmusic_api.models.login import QRCodeLoginEvents

        events = {
            QRCodeLoginEvents.SCAN: "waiting",
            QRCodeLoginEvents.CONF: "scanned",
            QRCodeLoginEvents.TIMEOUT: "expired",
            QRCodeLoginEvents.REFUSE: "refused",
            QRCodeLoginEvents.DONE: "done",
        }
        try:
            async with self._client() as client:
                stream = client.login.checking_mobile_qrcode(qr, deadline=anyio.current_time() + QR_TTL)
                async with contextlib.aclosing(stream) as results:
                    async for result in results:
                        status = events.get(result.event, "waiting")
                        if status == "done":
                            if result.credential is None or not result.credential.is_valid():
                                scan.error = "QQ 音乐扫码成功，但没拿到登录信息"
                            else:
                                scan.credential = result.credential.model_dump(by_alias=True)
                        scan.status = status
            if scan.status in ("waiting", "scanned"):
                scan.status = "expired"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.opt(exception=e).warning("[一起听] QQ 音乐 App 扫码出错")
            scan.error = _login_error(e)

    async def qr_poll(self, session: dict[str, Any]) -> QrState:
        sid = str(session.get("sid") or "")
        scan = self._app.get(sid)
        if scan is not None:
            return self._poll_app(sid, scan)
        return await self._poll_web(sid)

    def _poll_app(self, sid: str, scan: _AppScan) -> QrState:
        if scan.error:
            self._app.pop(sid, None)
            raise AccountError(scan.error)
        if scan.status == "done" and scan.credential:
            self._app.pop(sid, None)
            return QrState("done", credential=scan.credential)
        if scan.status in ("expired", "refused") or time.monotonic() - scan.started >= QR_TTL:
            self._app.pop(sid, None)
            scan.cancel()
            return QrState("expired" if scan.status != "refused" else "refused")
        return QrState(scan.status)

    async def _poll_web(self, sid: str) -> QrState:
        from qqmusic_api.models.login import QRCodeLoginEvents

        hit = self._qr.get(sid)
        if hit is None or time.monotonic() - hit[0] >= QR_TTL:
            return QrState("expired")
        patch_cookie_snapshot()
        try:
            async with self._client() as client:
                result = await client.login.check_qrcode(hit[1])
        except Exception as e:
            logger.opt(exception=e).warning("[一起听] QQ 音乐扫码状态检查出错")
            self._qr.pop(sid, None)
            raise AccountError(_login_error(e)) from e
        status = {
            QRCodeLoginEvents.SCAN: "waiting",
            QRCodeLoginEvents.CONF: "scanned",
            QRCodeLoginEvents.TIMEOUT: "expired",
            QRCodeLoginEvents.REFUSE: "refused",
            QRCodeLoginEvents.DONE: "done",
        }.get(result.event, "waiting")
        if status != "done":
            if status in ("expired", "refused"):
                self._qr.pop(sid, None)
            return QrState(status)
        self._qr.pop(sid, None)
        if result.credential is None or not result.credential.is_valid():
            raise AccountError("QQ 音乐扫码成功，但没拿到登录信息")
        return QrState("done", credential=result.credential.model_dump(by_alias=True))

    # ------------------------------------------------------------ 账号

    async def account(self, credential: dict[str, Any]) -> AccountInfo:
        try:
            async with self._client(credential) as client:
                if await client.login.check_expired():
                    raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码")
                vip_info = await client.user.get_vip_info()
                euin = str(credential.get("encryptUin") or "")
                name = ""
                if euin:
                    home = await client.user.get_homepage(euin)
                    name = home.base_info.name
        except CredentialExpired:
            raise
        except Exception as e:
            if self._expired(e):
                raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码") from e
            logger.opt(exception=e).warning("[一起听] QQ 音乐读取账号信息出错")
            raise AccountError(f"QQ 音乐接口出错（{type(e).__name__}）") from e
        ident = vip_info.identity
        vip = bool(ident.vip or ident.huge_vip or ident.eight or ident.twelve)
        return AccountInfo(str(credential.get("musicid") or ""), name, vip=vip)

    async def refresh(self, credential: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._client(credential) as client:
                fresh = await client.login.refresh_credential()
        except Exception as e:
            raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码") from e
        if not fresh.is_valid():
            raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码")
        return fresh.model_dump(by_alias=True)

    # ------------------------------------------------------------ 歌单

    async def playlists(self, *, credential: dict[str, Any] | None = None, account_id: str = "") -> list[RemotePlaylist]:
        uin = account_id or str((credential or {}).get("musicid") or "")
        if not uin.isdigit():
            raise AccountError("QQ 音乐要填 QQ 号")
        try:
            async with self._client(credential) as client:
                res = await client.user.get_created_songlist(int(uin))
        except Exception as e:
            if credential and self._expired(e):
                raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码") from e
            raise AccountError(f"QQ 音乐歌单读取失败（{type(e).__name__}）") from e
        found = []
        for p in res.playlists or []:
            if p.invalid:
                continue
            found.append(RemotePlaylist(
                remote_id=str(p.id),
                name="我喜欢" if p.dirid == LIKED_DIRID else p.title,
                track_count=int(p.songnum or 0),
                cover=https(p.picurl),
                # dir_show=0：歌单设为不公开
                private=int(p.dir_show if p.dir_show is not None else 1) == 0,
                liked=p.dirid == LIKED_DIRID,
                extra={"dirid": int(p.dirid or 0)},
            ))
        return found

    async def tracks(self, playlist: RemotePlaylist, *, credential: dict[str, Any] | None = None,
                     limit: int = MAX_PLAYLIST_TRACKS) -> list[Track]:
        try:
            async with self._client(credential) as client:
                request = client.songlist.get_detail(int(playlist.remote_id), dirid=int(playlist.extra.get("dirid")
                                                                                         or 0), num=100)
                songs = await request.collect_items(limit=limit)
        except Exception as e:
            if credential and self._expired(e):
                raise CredentialExpired("QQ 音乐登录已经失效，需要重新扫码") from e
            raise AccountError(f"QQ 音乐歌单读取失败（{type(e).__name__}）") from e
        return [t for t in (_track(s) for s in songs) if t][:limit]
