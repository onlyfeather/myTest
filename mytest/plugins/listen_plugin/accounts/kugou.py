"""酷狗账号（KuGouMusicApi）。

扫码成功返回 token + userid。凭证存 {"token", "userid", "dfid"}；请求时放在 Authorization 头里，不放 URL。

为什么用 Authorization 头（KuGouMusicApi server.js）：请求体里的 cookie 会整个替换掉服务自己注入的设备
cookie（KUGOU_API_MID 等，签名要用），Authorization 头则是合并进去。服务解析时只按 ";" 切、不去空格，
所以分隔符不能带空格，否则 " userid" 读不到，酷狗回 20018。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
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

# /login/qr/check 的 data.status
_QR = {0: "expired", 1: "waiting", 2: "scanned", 4: "done"}
_VIP_PRIVILEGE = 10


_BEIJING = timezone(timedelta(hours=8))
# 普通酷狗的会员到期时间：su_vip 豪华/超级 VIP，vip 普通 VIP，m 音乐包
_VIP_END_KEYS = ("su_vip_end_time", "vip_end_time", "m_end_time")


def vip_active(data: dict[str, Any]) -> bool:
    """/user/vip/detail 的 data 里有没有正在生效的会员。

    busi_vip 只是概念版的会员；实测（2026-10-29）普通酷狗的超级 VIP 在 busi_vip 里是空记录（is_vip=0），
    要看顶层的 is_vip 和各种会员的到期时间（北京时间，"2026-11-06 23:24:09"）。
    """
    if _int(data.get("is_vip")) == 1:
        return True
    if any(isinstance(v, dict) and _int(v.get("is_vip")) == 1 for v in data.get("busi_vip") or []):
        return True
    now = datetime.now(_BEIJING)
    for key in _VIP_END_KEYS:
        try:
            end = datetime.strptime(str(data.get(key) or ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BEIJING)
        except ValueError:
            continue
        if end > now:
            return True
    return False


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def auth_header(cred: dict[str, Any]) -> dict[str, str]:
    """登录信息放进 Authorization 头，格式 token=..;userid=..;dfid=..（不能有空格）。"""
    parts = [f"{k}={cred[k]}" for k in ("token", "userid", "dfid") if str(cred.get(k) or "").strip()]
    return {"Authorization": ";".join(parts)} if parts else {}


class KugouAccounts:
    platform: str = "kugou"
    login_kinds: tuple[str, ...] = ("app",)

    def __init__(self, base_url: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, path: str, cred: dict[str, Any] | None = None, **params: str | int) -> dict[str, Any]:
        # 和网易云一样：KuGouMusicApi 的缓存只看 URL，URL 里必须带随机数；登录信息放请求头，不进访问日志。
        query: dict[str, str | int] = {**params, "timestamp": time.time_ns(), "nonce": secrets.token_hex(8)}
        try:
            resp = await self._client.post(path, params=query, headers=auth_header(cred) if cred else None)
        except httpx.HTTPError as e:
            raise AccountError(f"酷狗接口连不上（{type(e).__name__}）") from e
        try:
            body = resp.json()
        except ValueError:
            raise AccountError(f"酷狗接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(body, dict):
            raise AccountError("酷狗接口返回格式不对")
        # 20010 / 20018：token 无效或过期
        if cred and int(body.get("error_code") or body.get("errcode") or 0) in (20010, 20018, 51002):
            raise CredentialExpired("酷狗登录已经失效，需要重新扫码")
        return body

    async def qr_start(self, kind: str = "app") -> QrStart:
        key = ((await self._call("/login/qr/key")).get("data") or {}).get("qrcode")
        if not key:
            raise AccountError("酷狗二维码生成失败")
        created = await self._call("/login/qr/create", key=str(key), qrimg="true")
        image = str((created.get("data") or {}).get("base64") or "")
        if not image.startswith("data:image/"):
            raise AccountError("酷狗二维码生成失败")
        dev = await self._call("/register/dev")
        return QrStart(session={"key": str(key), "dfid": str((dev.get("data") or {}).get("dfid") or "")},
                       image=image, hint="用酷狗音乐 App 扫码")

    async def qr_poll(self, session: dict[str, Any]) -> QrState:
        body = await self._call("/login/qr/check", key=str(session.get("key") or ""))
        data = body.get("data") or {}
        status = _QR.get(int(data.get("status") or 0), "expired")
        if status != "done":
            return QrState(status)
        token, userid = str(data.get("token") or ""), str(data.get("userid") or "")
        if not token or not userid.isdigit():
            raise AccountError("酷狗扫码成功，但没拿到登录信息")
        return QrState("done", credential={"token": token, "userid": userid, "dfid": session.get("dfid") or ""})

    async def account(self, credential: dict[str, Any]) -> AccountInfo:
        body = await self._call("/user/detail", credential)
        data = body.get("data") or {}
        if body.get("status") != 1 or not data:
            raise CredentialExpired("酷狗登录已经失效，需要重新扫码")
        vip = await self._call("/user/vip/detail", credential)
        is_vip = vip_active(vip.get("data") or {})
        return AccountInfo(str(credential.get("userid")), str(data.get("nickname") or ""), vip=is_vip)

    async def refresh(self, credential: dict[str, Any]) -> dict[str, Any]:
        body = await self._call("/login/token", credential)
        data = body.get("data") or {}
        token = str(data.get("token") or "")
        if body.get("status") != 1 or not token:
            raise CredentialExpired("酷狗登录已经失效，需要重新扫码")
        return {**credential, "token": token}

    async def playlists(self, *, credential: dict[str, Any] | None = None, account_id: str = "") -> list[RemotePlaylist]:
        if not credential:
            # 酷狗没有不登录就能看别人歌单的接口
            raise AccountError("酷狗只能扫码导入")
        found: list[RemotePlaylist] = []
        page = 1
        while page <= 10:
            body = await self._call("/user/playlist", credential, page=page, pagesize=100)
            info = ((body.get("data") or {}).get("info") or [])
            for p in info:
                gid = str(p.get("global_collection_id") or "")
                listid = str(p.get("listid") or "")
                if not gid and not listid:
                    continue
                # 只要自己建的：list_create_userid 是自己（收藏的别人的歌单不算）
                if str(p.get("list_create_userid") or credential.get("userid")) != str(credential.get("userid")):
                    continue
                found.append(RemotePlaylist(
                    remote_id=listid or gid,
                    name=str(p.get("name") or ""),
                    track_count=int(p.get("count") or 0),
                    cover=https(str(p.get("pic") or "").replace("{size}", "400")),
                    private=int(p.get("is_pri") or 0) == 1,
                    liked=str(p.get("name") or "") == "我喜欢" or int(p.get("is_def") or 0) == 2,
                    extra={"listid": listid, "gid": gid},
                ))
            if len(info) < 100:
                break
            page += 1
        return found

    async def tracks(self, playlist: RemotePlaylist, *, credential: dict[str, Any] | None = None,
                     limit: int = MAX_PLAYLIST_TRACKS) -> list[Track]:
        out: list[Track] = []
        page = 1
        listid = str(playlist.extra.get("listid") or "")
        while len(out) < limit and page <= 20:
            if listid and credential:
                body = await self._call("/playlist/track/all/new", credential, listid=listid, page=page, pagesize=100)
            else:
                body = await self._call("/playlist/track/all", credential, id=str(playlist.extra.get("gid") or
                                                                                 playlist.remote_id),
                                        page=page, pagesize=100)
            songs = ((body.get("data") or {}).get("songs") or (body.get("data") or {}).get("info") or [])
            for s in songs:
                t = _track(s)
                if t:
                    out.append(t)
            if len(songs) < 100:
                break
            page += 1
        return out[:limit]


def _track(song: dict[str, Any]) -> Track | None:
    file_hash = str(song.get("hash") or "").upper()
    if len(file_hash) != 32:
        return None
    name = str(song.get("name") or "")
    singers = "、".join(str(s.get("name") or "") for s in song.get("singerinfo") or [] if isinstance(s, dict))
    # 歌单接口的 name 是「歌手 - 歌名」
    if not singers and " - " in name:
        singers, _, name = name.partition(" - ")
    elif singers and name.startswith(f"{singers} - "):
        name = name[len(singers) + 3:]
    # 没有专辑的歌 album_id 是 0，取地址时照样传 0
    album_id = str(song.get("album_id") or (song.get("albuminfo") or {}).get("id") or "0")
    mix_id = str(song.get("mixsongid") or song.get("add_mixsongid") or song.get("audio_id") or "")
    privilege = int(next((g.get("privilege") for g in song.get("relate_goods") or [] if isinstance(g, dict)),
                         song.get("privilege") or 0) or 0)
    return Track(
        platform="kugou",
        id=file_hash,
        title=name,
        artists=singers,
        album=str((song.get("albuminfo") or {}).get("name") or ""),
        cover=https(str(song.get("cover") or "").replace("{size}", "400")),
        duration_ms=int(song.get("timelen") or 0),
        vip=privilege == _VIP_PRIVILEGE,
        file_id=f"{album_id}_{mix_id}" if album_id.isdigit() and mix_id.isdigit() else "",
    )
