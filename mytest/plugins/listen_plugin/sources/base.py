"""音源接口和公共数据类型。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

PLATFORMS = ("qq", "netease", "kugou")
PLATFORM_NAMES = {"qq": "QQ音乐", "netease": "网易云音乐", "kugou": "酷狗音乐"}

# 歌曲引用 platform:id。id 只允许字母数字和下划线，引用会出现在 URL 和缓存文件名里。
_REF = re.compile(r"^(qq|netease|kugou):([A-Za-z0-9_]{1,64})$")
_FILE_ID = re.compile(r"^[A-Za-z0-9_]{0,64}$")


class SourceError(Exception):
    """音源出错。message 直接显示给用户。"""


class SourceUnavailable(SourceError):
    """平台本身出了问题（连不上、返回格式不对），计入熔断。“这首没有地址”这类正常结果用 SourceError。"""


def https(url: object) -> str:
    """平台给的封面和音频地址多是 http；三家的 CDN 都支持同一路径的 https，换掉以免被浏览器当成混合内容拦掉。"""
    text = str(url or "")
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text if text.startswith("https://") else ""


def parse_ref(ref: str) -> tuple[str, str]:
    m = _REF.match(ref or "")
    if not m:
        raise SourceError("歌曲编号格式不对")
    return m.group(1), m.group(2)


@dataclass(frozen=True)
class Track:
    platform: str
    id: str
    title: str
    artists: str
    album: str = ""
    cover: str = ""
    duration_ms: int = 0
    vip: bool = False
    # 平台取播放地址要用的文件编号（QQ 音乐的 media_mid），和歌曲一起保存，重启后不用再查。
    file_id: str = ""

    @property
    def ref(self) -> str:
        return f"{self.platform}:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": self.ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        platform, track_id = parse_ref(f"{data.get('platform')}:{data.get('id')}")
        return cls(
            platform=platform,
            id=track_id,
            title=str(data.get("title") or "")[:200],
            artists=str(data.get("artists") or "")[:200],
            album=str(data.get("album") or "")[:200],
            cover=str(data.get("cover") or "") if str(data.get("cover") or "").startswith("https://") else "",
            duration_ms=max(0, int(data.get("duration_ms") or 0)),
            vip=bool(data.get("vip")),
            file_id=str(data.get("file_id") or "") if _FILE_ID.match(str(data.get("file_id") or "")) else "",
        )

    def label(self) -> str:
        return f"{self.title} - {self.artists}" if self.artists else self.title


@dataclass(frozen=True)
class Stream:
    url: str
    expires_in: int  # 秒
    bitrate: int  # kbps


class MusicSource(Protocol):
    platform: str

    async def search(self, keyword: str, limit: int = 20) -> list[Track]: ...

    async def stream(self, track: Track, bitrate: int = 320) -> Stream: ...

    async def lyrics(self, track: Track) -> tuple[str, str]:
        """返回 (LRC 原文, LRC 翻译)，没有时为空字符串。"""
        ...
