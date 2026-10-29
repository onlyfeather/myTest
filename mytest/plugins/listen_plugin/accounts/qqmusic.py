"""QQ 音乐账号（qqmusic-api-python，进程内调用）。支持 QQ 扫码和微信扫码。

凭证是库的 Credential（pydantic 模型）导出成的 dict。二维码会话里的 QR 对象（含图片字节）不能放进 JSON，
所以只在内存里按会话 ID 保存，5 分钟后清掉。
"""

from __future__ import annotations

import base64
import secrets
import time
from typing import Any

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
    login_kinds: tuple[str, ...] = ("qq", "wx")

    def __init__(self) -> None:
        self._qr: dict[str, tuple[float, Any]] = {}

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

    # ------------------------------------------------------------ 扫码

    async def qr_start(self, kind: str = "qq") -> QrStart:
        from qqmusic_api.models.login import QRLoginType

        login_type = QRLoginType.WX if kind == "wx" else QRLoginType.QQ
        now = time.monotonic()
        self._qr = {k: v for k, v in self._qr.items() if now - v[0] < QR_TTL}
        try:
            async with self._client() as client:
                qr = await client.login.get_qrcode(login_type)
        except Exception as e:
            raise AccountError(f"QQ 音乐二维码生成失败（{type(e).__name__}）") from e
        sid = secrets.token_urlsafe(16)
        self._qr[sid] = (now, qr)
        image = f"data:{qr.mimetype or 'image/png'};base64,{base64.b64encode(qr.data).decode()}"
        return QrStart(session={"sid": sid}, image=image,
                       hint="用微信扫码" if kind == "wx" else "用 QQ 扫码（手机 QQ 或者 QQ 音乐 App 都行）")

    async def qr_poll(self, session: dict[str, Any]) -> QrState:
        from qqmusic_api import LoginError
        from qqmusic_api.models.login import QRCodeLoginEvents

        hit = self._qr.get(str(session.get("sid") or ""))
        if hit is None or time.monotonic() - hit[0] >= QR_TTL:
            return QrState("expired")
        try:
            async with self._client() as client:
                result = await client.login.check_qrcode(hit[1])
        except LoginError as e:
            self._qr.pop(str(session.get("sid")), None)
            raise AccountError(f"QQ 音乐登录失败：{str(e)[:80]}") from e
        except Exception as e:
            raise AccountError(f"QQ 音乐接口出错（{type(e).__name__}）") from e
        status = {
            QRCodeLoginEvents.SCAN: "waiting",
            QRCodeLoginEvents.CONF: "scanned",
            QRCodeLoginEvents.TIMEOUT: "expired",
            QRCodeLoginEvents.REFUSE: "refused",
            QRCodeLoginEvents.DONE: "done",
        }.get(result.event, "waiting")
        if status != "done":
            if status in ("expired", "refused"):
                self._qr.pop(str(session.get("sid")), None)
            return QrState(status)
        self._qr.pop(str(session.get("sid")), None)
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
