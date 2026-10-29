"""QQ 音乐（qqmusic-api-python，进程内调用）。

搜索、歌词不需要登录。会员歌取地址时用 bot 的会员账号凭证（WebUI「一起听」里扫码登录）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import SourceError, SourceUnavailable, Stream, Track, VipCredential

# 播放地址的 CDN。官方调度接口给的都是 http，这里用同一批里支持 https 的域名，浏览器不会当成混合内容拦掉。
CDN = "https://ws.stream.qqmusic.qq.com/"
_MID = re.compile(r"^[A-Za-z0-9]{10,20}$")


def _cover(album_mid: str) -> str:
    return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""


def _is_vip(pay: Any) -> bool:
    return bool(pay and (pay.pay_play or pay.pay_month))


class QQMusicSource(VipCredential):
    platform = "qq"

    def __init__(self) -> None:
        super().__init__()
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get(self) -> Any:
        async with self._lock:
            if self._client is None:
                from qqmusic_api import Client

                self._client = Client()
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        client = await self._get()
        try:
            res = await client.search.search_by_type(keyword, num=min(max(limit, 1), 50), highlight=False)
        except Exception as e:
            raise SourceUnavailable(f"QQ 音乐搜索失败（{type(e).__name__}）") from e
        tracks = []
        for song in res.song or []:
            if not _MID.match(song.mid or ""):
                continue
            album_mid = song.album.mid if song.album else ""
            tracks.append(
                Track(
                    platform="qq",
                    id=song.mid,
                    title=song.title or song.name,
                    artists="、".join(s.name for s in song.singer or []),
                    album=song.album.name if song.album else "",
                    cover=_cover(album_mid if _MID.match(album_mid or "") else ""),
                    duration_ms=int(song.interval or 0) * 1000,
                    vip=_is_vip(song.pay),
                    file_id=(song.file.media_mid if song.file and _MID.match(song.file.media_mid or "") else ""),
                )
            )
        return tracks

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        from qqmusic_api import Credential
        from qqmusic_api.modules.song import SongFileInfo, SongFileType

        client = await self._get()
        file_type = SongFileType.MP3_320 if bitrate >= 320 else SongFileType.MP3_128
        info = SongFileInfo(mid=track.id, media_mid=track.file_id or None)
        raw = self.credential() if track.vip else None
        cred = Credential.model_validate(raw) if raw else None
        try:
            res = await client.song.get_song_urls([info], file_type=file_type, credential=cred)
        except Exception as e:
            if cred is not None and type(e).__name__ in ("CredentialExpiredError", "CredentialInvalidError"):
                self.credential_failed("QQ 音乐会员账号登录失效了")
                raise SourceError("QQ 音乐会员账号登录失效了") from e
            raise SourceUnavailable(f"QQ 音乐取播放地址失败（{type(e).__name__}）") from e
        item = res.data[0] if res.data else None
        if (item is None or not item.purl) and bitrate >= 320:
            return await self.stream(track, bitrate=128)
        if item is None or not item.purl:
            if cred is not None:
                self.credential_failed("QQ 音乐会员账号取不到会员歌，可能登录失效或者会员过期了")
            raise SourceError("QQ 音乐没有给播放地址，可能是会员歌或者已经下架")
        return Stream(url=CDN + item.purl, expires_in=int(res.expiration or 7200), bitrate=min(bitrate, 320))

    async def lyrics(self, track: Track) -> tuple[str, str]:
        client = await self._get()
        try:
            res = await client.lyric.get_lyric(track.id, trans=True)
        except Exception as e:
            raise SourceUnavailable(f"QQ 音乐歌词获取失败（{type(e).__name__}）") from e
        return res.lyric or "", res.trans or ""
