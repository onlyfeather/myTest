"""平台账号：扫码登录、刷新凭证、读歌单。

凭证在这一层是普通 dict（各平台自己的字段），存数据库前由 crypto.Vault 加密，用完即丢，不出现在日志和返回值里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..sources.base import Track


class AccountError(Exception):
    """登录、刷新或读歌单失败。message 直接显示给用户，不能包含凭证。"""


class CredentialExpired(AccountError):
    """凭证失效，需要重新扫码。"""


@dataclass(frozen=True)
class QrStart:
    session: dict[str, Any]  # 轮询时原样传回；不发给浏览器
    image: str  # data:image/png;base64,... 浏览器直接显示
    hint: str = ""  # 例如“用 QQ 扫码”“用微信扫码”


@dataclass(frozen=True)
class QrState:
    status: str  # waiting / scanned / done / expired / refused
    credential: dict[str, Any] | None = None
    message: str = ""


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    name: str
    vip: bool = False


@dataclass(frozen=True)
class RemotePlaylist:
    remote_id: str
    name: str
    track_count: int
    cover: str = ""
    private: bool = False  # 平台上是私密歌单
    liked: bool = False  # 平台上的“我喜欢”
    extra: dict[str, Any] = field(default_factory=dict)  # 读歌曲时要用的附加参数（QQ 音乐的 dirid 等）


class PlatformAccounts(Protocol):
    platform: str
    login_kinds: tuple[str, ...]  # 支持的扫码方式，比如 ("qq", "wx")

    async def qr_start(self, kind: str) -> QrStart: ...

    async def qr_poll(self, session: dict[str, Any]) -> QrState: ...

    async def account(self, credential: dict[str, Any]) -> AccountInfo:
        """凭证对应的账号（ID、昵称、是否会员）。凭证失效抛 CredentialExpired。"""
        ...

    async def refresh(self, credential: dict[str, Any]) -> dict[str, Any]:
        """刷新凭证，返回新凭证。刷新不了抛 CredentialExpired。"""
        ...

    async def playlists(self, *, credential: dict[str, Any] | None = None, account_id: str = "") -> list[RemotePlaylist]:
        """有凭证时列出全部歌单（含私密）；只有 account_id 时列公开歌单。"""
        ...

    async def tracks(self, playlist: RemotePlaylist, *, credential: dict[str, Any] | None = None,
                     limit: int = 1000) -> list[Track]: ...


MAX_PLAYLIST_TRACKS = 1000
