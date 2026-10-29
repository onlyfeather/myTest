"""酷狗音乐：调用自己部署的 KuGouMusicApi（Node 服务）。

KUGOU_API_URL=http://kugou-api:3000

取播放地址前必须先调 /register/dev 拿 dfid，否则返回「本次请求需要验证」。dfid 缓存在内存里，失效时重新注册。
酷狗的歌曲 ID 是 FileHash（32 位十六进制）；取地址还需要专辑 ID 和 MixSongID，一起存在 file_id 里。
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time

import httpx

from .base import SourceError, SourceUnavailable, Stream, Track, VipCredential, https

_HASH = re.compile(r"^[0-9A-Fa-f]{32}$")
# Privilege：0 免费、8 免费（部分音质）、10 会员
_VIP_PRIVILEGE = 10


def _cover(image: str) -> str:
    return https(image.replace("{size}", "400")) if image else ""


class KugouSource(VipCredential):
    platform = "kugou"

    def __init__(self, base_url: str, *, timeout: float = 8.0, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self._dfid = ""
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: str | int) -> dict:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"酷狗接口连不上（{type(e).__name__}）") from e
        try:
            data = resp.json()
        except ValueError:
            raise SourceUnavailable(f"酷狗接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(data, dict):
            raise SourceUnavailable("酷狗接口返回格式不对")
        return data

    async def _device(self, *, renew: bool = False) -> str:
        async with self._lock:
            if renew or not self._dfid:
                data = await self._get("/register/dev")
                dfid = str((data.get("data") or {}).get("dfid") or "")
                if not dfid:
                    raise SourceError("酷狗设备注册失败")
                self._dfid = dfid
            return self._dfid

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        data = await self._get("/search", keywords=keyword, pagesize=min(max(limit, 1), 50))
        if data.get("status") != 1:
            raise SourceError(f"酷狗搜索失败：{str(data.get('error_msg') or data.get('error_code'))[:80]}")
        tracks = []
        for song in (data.get("data") or {}).get("lists") or []:
            file_hash = str(song.get("FileHash") or "")
            album_id = str(song.get("AlbumID") or "")
            mix_id = str(song.get("MixSongID") or "")
            if not _HASH.match(file_hash):
                continue
            tracks.append(
                Track(
                    platform="kugou",
                    id=file_hash.upper(),
                    title=str(song.get("SongName") or ""),
                    artists="、".join(str(s.get("name") or "") for s in song.get("Singers") or [])
                    or str(song.get("SingerName") or ""),
                    album=str(song.get("AlbumName") or ""),
                    cover=_cover(str(song.get("Image") or "")),
                    duration_ms=int(song.get("Duration") or 0) * 1000,
                    vip=int(song.get("Privilege") or 0) == _VIP_PRIVILEGE,
                    file_id=f"{album_id}_{mix_id}" if album_id.isdigit() and mix_id.isdigit() else "",
                )
            )
        return tracks

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        album_id, _, mix_id = track.file_id.partition("_")
        cred = self.credential() if track.vip else None
        for attempt in range(2):
            params = {
                "hash": track.id,
                "album_id": album_id or "0",
                "album_audio_id": mix_id or "0",
                "quality": "320" if bitrate >= 320 else "128",
            }
            if cred:
                # 会员账号的 token 放 Authorization 头（不进 KuGouMusicApi 的访问日志），URL 带随机数绕开缓存。
                # 格式和原因见 accounts/kugou.py 的 auth_header。
                data = await self._post("/song/url", cred, **params)
            else:
                dfid = await self._device(renew=attempt > 0)
                data = await self._get("/song/url", cookie=f"dfid={dfid}", **params)
            if data.get("errcode") == 20028 and not cred:  # 需要验证：dfid 失效，重新注册一次
                continue
            if cred and int(data.get("error_code") or data.get("errcode") or 0) in (20010, 20018, 51002):
                self.credential_failed("酷狗会员账号登录失效了")
                raise SourceError("酷狗会员账号登录失效了")
            urls = [https(u) for u in (data.get("url") or []) + (data.get("backupUrl") or []) if u]
            if data.get("status") == 1 and urls:
                return Stream(url=urls[0], expires_in=1800, bitrate=320 if bitrate >= 320 else 128)
            if bitrate >= 320:
                return await self.stream(track, bitrate=128)
            if cred:
                self.credential_failed("酷狗会员账号取不到会员歌，可能会员过期了")
            break
        raise SourceError("酷狗没有给播放地址，可能是会员歌或者已经下架")

    async def _post(self, path: str, cred: dict, **params: str | int) -> dict:
        from ..accounts.kugou import auth_header

        query: dict[str, str | int] = {**params, "timestamp": time.time_ns(), "nonce": secrets.token_hex(8)}
        try:
            resp = await self._client.post(path, params=query, headers=auth_header(cred))
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"酷狗接口连不上（{type(e).__name__}）") from e
        try:
            data = resp.json()
        except ValueError:
            raise SourceUnavailable(f"酷狗接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(data, dict):
            raise SourceUnavailable("酷狗接口返回格式不对")
        return data

    async def lyrics(self, track: Track) -> tuple[str, str]:
        found = await self._get("/search/lyric", hash=track.id)
        candidates = found.get("candidates") or []
        if not candidates:
            return "", ""
        best = candidates[0]
        data = await self._get(
            "/lyric", id=str(best.get("id") or ""), accesskey=str(best.get("accesskey") or ""), fmt="lrc", decode="true"
        )
        return str(data.get("decodeContent") or ""), ""
