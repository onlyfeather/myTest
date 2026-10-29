"""网易云账号（api-enhanced）。

凭证是登录后的 cookie 字符串，所有需要登录的请求都用 POST 放在请求体的 cookie 字段里。
不放 URL：api-enhanced 会把请求 URL 打到日志里。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx

from ..sources.base import Track, https
from .base import (
    MAX_PLAYLIST_TRACKS,
    AccountError,
    AccountInfo,
    CredentialExpired,
    QrStart,
    QrState,
    RemotePlaylist,
)

_VIP_FEE = {1, 4}
# /login/qr/check 的状态码
_QR = {800: "expired", 801: "waiting", 802: "scanned", 803: "done"}


def _track(song: dict[str, Any]) -> Track | None:
    song_id = str(song.get("id") or "")
    if not song_id.isdigit():
        return None
    album = song.get("al") or {}
    pic = https(album.get("picUrl"))
    return Track(
        platform="netease",
        id=song_id,
        title=str(song.get("name") or ""),
        artists="、".join(str(a.get("name") or "") for a in song.get("ar") or []),
        album=str(album.get("name") or ""),
        cover=pic + "?param=300y300" if pic else "",
        duration_ms=int(song.get("dt") or 0),
        vip=int(song.get("fee") or 0) in _VIP_FEE,
    )


class NeteaseAccounts:
    platform: str = "netease"
    login_kinds: tuple[str, ...] = ("app",)

    def __init__(self, base_url: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, path: str, cookie: str = "", **params: str | int) -> dict[str, Any]:
        # 参数放 URL，cookie 放请求体（不进 api-enhanced 的访问日志）。
        # api-enhanced 的 2 分钟缓存只按 URL 和请求头 cookie 区分，不看请求体：URL 里必须带上随机数，
        # 否则不同人的私密歌单会命中同一份缓存。
        query: dict[str, str | int] = {**params, "timestamp": time.time_ns(), "nonce": secrets.token_hex(8)}
        try:
            resp = await self._client.post(path, params=query, data={"cookie": cookie} if cookie else None)
        except httpx.HTTPError as e:
            raise AccountError(f"网易云接口连不上（{type(e).__name__}）") from e
        try:
            body = resp.json()
        except ValueError:
            raise AccountError(f"网易云接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(body, dict):
            raise AccountError("网易云接口返回格式不对")
        if body.get("code") == 301 or resp.status_code == 301:
            raise CredentialExpired("网易云登录已经失效，需要重新扫码")
        return body

    # ------------------------------------------------------------ 扫码

    async def qr_start(self, kind: str = "app") -> QrStart:
        key = ((await self._call("/login/qr/key")).get("data") or {}).get("unikey")
        if not key:
            raise AccountError("网易云二维码生成失败")
        created = await self._call("/login/qr/create", key=str(key), qrimg="true")
        image = str((created.get("data") or {}).get("qrimg") or "")
        if not image.startswith("data:image/"):
            raise AccountError("网易云二维码生成失败")
        return QrStart(session={"key": str(key)}, image=image, hint="用网易云音乐 App 扫码")

    async def qr_poll(self, session: dict[str, Any]) -> QrState:
        body = await self._call("/login/qr/check", key=str(session.get("key") or ""), noCookie="true")
        status = _QR.get(int(body.get("code") or 0))
        if status is None:
            return QrState("expired", message=str(body.get("message") or "二维码失效了")[:80])
        if status != "done":
            return QrState(status)
        cookie = str(body.get("cookie") or "")
        if "MUSIC_U=" not in cookie:
            raise AccountError("网易云扫码成功，但没拿到登录信息")
        return QrState("done", credential={"cookie": cookie})

    # ------------------------------------------------------------ 账号

    async def account(self, credential: dict[str, Any]) -> AccountInfo:
        body = await self._call("/user/account", cookie=str(credential.get("cookie") or ""))
        profile = body.get("profile") or {}
        account = body.get("account") or {}
        uid = str(profile.get("userId") or account.get("id") or "")
        if not uid.isdigit():
            raise CredentialExpired("网易云登录已经失效，需要重新扫码")
        return AccountInfo(uid, str(profile.get("nickname") or ""), vip=int(account.get("vipType") or 0) > 0)

    async def refresh(self, credential: dict[str, Any]) -> dict[str, Any]:
        cookie = str(credential.get("cookie") or "")
        body = await self._call("/login/refresh", cookie=cookie)
        fresh = str(body.get("cookie") or "")
        # 文档说扫码登录的 cookie 刷新不了；刷新失败时只要原来的还能用就继续用。
        if body.get("code") == 200 and "MUSIC_U=" in fresh:
            return {"cookie": _merge_cookie(cookie, fresh)}
        await self.account(credential)  # 失效就抛 CredentialExpired
        return credential

    # ------------------------------------------------------------ 歌单

    async def playlists(self, *, credential: dict[str, Any] | None = None, account_id: str = "") -> list[RemotePlaylist]:
        cookie = str((credential or {}).get("cookie") or "")
        uid = account_id
        if not uid and credential:
            uid = (await self.account(credential)).account_id
        if not str(uid).isdigit():
            raise AccountError("网易云用户 ID 应该是一串数字")
        found: list[RemotePlaylist] = []
        offset = 0
        while len(found) < 500:
            body = await self._call("/user/playlist", cookie=cookie, uid=str(uid), limit=100, offset=offset)
            items = body.get("playlist") or []
            for p in items:
                # 只要这个人自己建的歌单（收藏别人的歌单不算他的）
                if str((p.get("creator") or {}).get("userId")) != str(uid):
                    continue
                found.append(RemotePlaylist(
                    remote_id=str(p.get("id")),
                    name=str(p.get("name") or ""),
                    track_count=int(p.get("trackCount") or 0),
                    cover=https(p.get("coverImgUrl")),
                    private=int(p.get("privacy") or 0) == 10,
                    liked=int(p.get("specialType") or 0) == 5,
                ))
            if not body.get("more") or not items:
                break
            offset += len(items)
        if not found and not cookie:
            detail = await self._call("/user/detail", uid=str(uid))
            if detail.get("code") == 404 or not detail.get("profile"):
                raise AccountError("没有这个网易云用户")
        return found

    async def tracks(self, playlist: RemotePlaylist, *, credential: dict[str, Any] | None = None,
                     limit: int = MAX_PLAYLIST_TRACKS) -> list[Track]:
        cookie = str((credential or {}).get("cookie") or "")
        out: list[Track] = []
        offset = 0
        while len(out) < limit:
            page = min(500, limit - len(out))
            body = await self._call("/playlist/track/all", cookie=cookie, id=playlist.remote_id,
                                    limit=page, offset=offset)
            songs = body.get("songs") or []
            out.extend(t for t in (_track(s) for s in songs) if t)
            if len(songs) < page:
                break
            offset += len(songs)
        return out[:limit]


def _merge_cookie(old: str, new: str) -> str:
    def parse(text: str) -> dict[str, str]:
        out = {}
        for part in text.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name and name.lower() not in {"path", "expires", "max-age", "domain", "httponly", "secure"}:
                out[name] = value
        return out

    return "; ".join(f"{k}={v}" for k, v in {**parse(old), **parse(new)}.items())
