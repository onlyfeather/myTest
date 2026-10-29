"""网易云音乐：调用自己部署的 api-enhanced（NeteaseCloudMusicApi 的延续项目，Node 服务）。

NETEASE_API_URL=http://netease-api:3000
"""

from __future__ import annotations

import secrets
import time

import httpx

from .base import SourceError, SourceUnavailable, Stream, Track, VipCredential, https

# fee: 0 免费、8 免费（低音质）、1 会员、4 付费专辑。
_VIP_FEE = {1, 4}


class NeteaseSource(VipCredential):
    platform = "netease"

    def __init__(self, base_url: str, *, timeout: float = 8.0, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: str | int) -> dict:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"网易云接口连不上（{type(e).__name__}）") from e
        try:
            data = resp.json()
        except ValueError:
            raise SourceUnavailable(f"网易云接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(data, dict) or data.get("code") not in (200, None):
            raise SourceError(f"网易云接口出错：{str(data.get('message') or data.get('msg') or data.get('code'))[:80]}")
        return data

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        data = await self._get("/cloudsearch", keywords=keyword, limit=min(max(limit, 1), 50))
        tracks = []
        for song in (data.get("result") or {}).get("songs") or []:
            song_id = str(song.get("id") or "")
            if not song_id.isdigit():
                continue
            album = song.get("al") or {}
            tracks.append(
                Track(
                    platform="netease",
                    id=song_id,
                    title=str(song.get("name") or ""),
                    artists="、".join(str(a.get("name") or "") for a in song.get("ar") or []),
                    album=str(album.get("name") or ""),
                    cover=https(album.get("picUrl")) + ("?param=300y300" if album.get("picUrl") else ""),
                    duration_ms=int(song.get("dt") or 0),
                    vip=int(song.get("fee") or 0) in _VIP_FEE,
                )
            )
        return tracks

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        level = "exhigh" if bitrate >= 320 else "standard"
        cred = self.credential()
        cookie = str((cred or {}).get("cookie") or "")
        if cookie:
            # 带会员登录时用 POST，cookie 放请求体，URL 带随机数绕开 api-enhanced 的缓存（见 accounts/netease.py）
            data = await self._post("/song/url/v1", cookie, id=track.id, level=level)
        else:
            data = await self._get("/song/url/v1", id=track.id, level=level)
        item = (data.get("data") or [{}])[0]
        url = https(item.get("url"))
        # 非会员拿会员歌时会返回 30 秒试听（freeTrialInfo 不为空），当作拿不到。
        if not url or item.get("freeTrialInfo"):
            if cookie and track.vip:
                self.credential_failed("网易云会员账号取不到会员歌，可能登录失效或者会员过期了")
            raise SourceError("网易云没有给播放地址，可能是会员歌或者已经下架")
        return Stream(url=url, expires_in=int(item.get("expi") or 1200), bitrate=min(int(item.get("br") or 0) // 1000, 320))

    async def _post(self, path: str, cookie: str, **params: str | int) -> dict:
        query: dict[str, str | int] = {**params, "timestamp": time.time_ns(), "nonce": secrets.token_hex(8)}
        try:
            resp = await self._client.post(path, params=query, data={"cookie": cookie})
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"网易云接口连不上（{type(e).__name__}）") from e
        try:
            data = resp.json()
        except ValueError:
            raise SourceUnavailable(f"网易云接口返回格式不对（HTTP {resp.status_code}）") from None
        if not isinstance(data, dict):
            raise SourceUnavailable("网易云接口返回格式不对")
        if data.get("code") == 301:
            self.credential_failed("网易云会员账号登录失效了")
            raise SourceError("网易云会员账号登录失效了")
        return data

    async def lyrics(self, track: Track) -> tuple[str, str]:
        data = await self._get("/lyric", id=track.id)
        return (data.get("lrc") or {}).get("lyric") or "", (data.get("tlyric") or {}).get("lyric") or ""
