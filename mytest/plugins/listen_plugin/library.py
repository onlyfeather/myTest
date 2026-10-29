"""个人曲库：喜欢的音乐、导入的歌单、扫码登录、凭证加密保存和每日同步。也管 bot 自己的会员账号。

凭证的流向：扫码成功 → Vault 加密 → SQLite。要用时解密到内存，用完不保留；不写日志、不返回给浏览器。
群友的凭证只用来读他自己的歌单（已确认）；放歌只用 bot 的会员账号（owner = "bot"）。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger

from .accounts.base import (
    MAX_PLAYLIST_TRACKS,
    AccountError,
    AccountInfo,
    CredentialExpired,
    PlatformAccounts,
    QrStart,
    RemotePlaylist,
)
from .crypto import CryptoError, Sealed, Vault
from .sources.base import PLATFORM_NAMES, Track
from .store import CredentialRow, Store
from .sync import now_ms

BOT = "bot"
QR_SESSION_TTL = 300.0
MAX_QR_SESSIONS = 200
SYNC_INTERVAL_MS = 24 * 3600 * 1000
REFRESH_INTERVAL_MS = 12 * 3600 * 1000


class LibraryError(Exception):
    """message 直接显示给用户。"""


@dataclass
class _QrSession:
    owner: str
    platform: str
    kind: str
    session: dict[str, Any]
    created: float
    status: str = "waiting"
    message: str = ""
    # 扫码成功后，等用户勾选歌单期间先把歌单列表放在这里（不含凭证）
    playlists: list[RemotePlaylist] = field(default_factory=list)


class Library:
    def __init__(
        self,
        store: Store,
        accounts: dict[str, PlatformAccounts],
        vault: Vault | None,
        *,
        clock: Callable[[], int] = now_ms,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.accounts = accounts
        self.vault = vault
        self._clock = clock
        self._monotonic = monotonic
        self._qr: dict[str, _QrSession] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        # 已解密的 bot 凭证，缓存在内存里，避免每次取播放地址都解密
        self._bot_cache: dict[str, dict[str, Any]] = {}
        self.on_bot_credential_changed: Callable[[str], None] = lambda platform: None

    # ------------------------------------------------------------ 平台

    def platforms(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p,
                "name": PLATFORM_NAMES.get(p, p),
                "login_kinds": list(a.login_kinds) if self.vault else [],
                "uid_import": p != "kugou",
                "uid_label": {"qq": "QQ 号", "netease": "网易云用户 ID"}.get(p, ""),
            }
            for p, a in self.accounts.items()
        ]

    def _account(self, platform: str) -> PlatformAccounts:
        acc = self.accounts.get(platform)
        if acc is None:
            raise LibraryError(f"{PLATFORM_NAMES.get(platform, platform)}没有接入")
        return acc

    # ------------------------------------------------------------ 凭证

    def _lock(self, owner: str, platform: str) -> asyncio.Lock:
        return self._locks.setdefault((owner, platform), asyncio.Lock())

    def _save(self, owner: str, platform: str, cred: dict[str, Any], info: AccountInfo) -> None:
        if self.vault is None:
            raise LibraryError("没有配置 LISTEN_CREDENTIAL_KEY，不能保存登录")
        sealed = self.vault.seal(owner, platform, cred)
        self.store.save_credential(owner, platform, CredentialRow(
            sealed.ciphertext, sealed.nonce, sealed.key_id, info.account_id, info.name, info.vip,
        ), self._clock())
        if owner == BOT:
            self._bot_cache[platform] = cred
            self.on_bot_credential_changed(platform)

    def _load(self, owner: str, platform: str) -> tuple[dict[str, Any], CredentialRow] | None:
        if self.vault is None:
            return None
        row = self.store.credential(owner, platform)
        if row is None:
            return None
        try:
            cred = self.vault.open(owner, platform, Sealed(row.ciphertext, row.nonce, row.key_id))
        except CryptoError as e:
            self.store.mark_credential(owner, platform, "expired", str(e))
            return None
        if self.vault.needs_rotation(Sealed(row.ciphertext, row.nonce, row.key_id)):
            sealed = self.vault.seal(owner, platform, cred)
            self.store.save_credential(owner, platform, CredentialRow(
                sealed.ciphertext, sealed.nonce, sealed.key_id, row.account_id, row.account_name, row.vip,
            ), row.refreshed_at)
        return cred, row

    def bot_credential(self, platform: str) -> dict[str, Any] | None:
        """取播放地址时用。只返回状态正常的。"""
        if platform in self._bot_cache:
            return self._bot_cache[platform]
        loaded = self._load(BOT, platform)
        if loaded is None or loaded[1].status != "ok":
            return None
        self._bot_cache[platform] = loaded[0]
        return loaded[0]

    def bot_credential_failed(self, platform: str, message: str) -> None:
        """取播放地址时平台说登录失效了：标记失效，WebUI 上提示重新扫码。"""
        self._bot_cache.pop(platform, None)
        self.store.mark_credential(BOT, platform, "expired", message)
        self.on_bot_credential_changed(platform)

    def credential_status(self, owner: str) -> list[dict[str, Any]]:
        out = []
        for platform in self.accounts:
            row = self.store.credential(owner, platform)
            linked = self.store.linked_account(owner, platform) if owner != BOT else None
            out.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "logged_in": row is not None and row.status == "ok",
                "expired": row is not None and row.status != "ok",
                "message": row.status_message if row else "",
                "account_name": (row.account_name if row else "") or (linked[1] if linked else ""),
                "account_id": (row.account_id if row else "") or (linked[0] if linked else ""),
                "vip": bool(row and row.vip),
                "refreshed_at": row.refreshed_at if row else 0,
            })
        return out

    def logout(self, owner: str, platform: str) -> None:
        """解绑：立即删除凭证。已经导入的歌单保留，以后按公开歌单同步（如果能的话）。"""
        self.store.delete_credential(owner, platform)
        if owner == BOT:
            self._bot_cache.pop(platform, None)
            self.on_bot_credential_changed(platform)

    def forget_user(self, qq: str) -> None:
        self.store.forget_user(qq)

    # ------------------------------------------------------------ 扫码

    def _purge_qr(self) -> None:
        now = self._monotonic()
        self._qr = {k: v for k, v in self._qr.items() if now - v.created < QR_SESSION_TTL}

    async def qr_start(self, owner: str, platform: str, kind: str) -> tuple[str, QrStart]:
        if self.vault is None:
            raise LibraryError("扫码登录没有开启：管理员还没配置 LISTEN_CREDENTIAL_KEY")
        acc = self._account(platform)
        if kind not in acc.login_kinds:
            kind = acc.login_kinds[0]
        self._purge_qr()
        # 同一个人同时只保留一个二维码
        self._qr = {k: v for k, v in self._qr.items() if v.owner != owner}
        if len(self._qr) >= MAX_QR_SESSIONS:
            raise LibraryError("扫码的人太多了，稍后再试")
        try:
            start = await acc.qr_start(kind)
        except AccountError as e:
            raise LibraryError(str(e)) from e
        except Exception as e:
            logger.opt(exception=e).warning("[一起听] {}二维码生成出错", PLATFORM_NAMES.get(platform, platform))
            raise LibraryError(f"二维码生成失败（{type(e).__name__}），详情见 NoneBot 日志") from e
        sid = secrets.token_urlsafe(18)
        self._qr[sid] = _QrSession(owner, platform, kind, start.session, self._monotonic())
        return sid, start

    async def qr_poll(self, owner: str, sid: str) -> dict[str, Any]:
        """返回 {status, message, playlists?}。扫码成功时保存凭证；群友的会顺便列出他的歌单让他勾选。"""
        self._purge_qr()
        qs = self._qr.get(sid)
        if qs is None or qs.owner != owner:
            return {"status": "expired", "message": "二维码过期了，重新获取一个"}
        if qs.status == "done":
            return {"status": "done", "playlists": [_remote_dict(p) for p in qs.playlists]}
        acc = self._account(qs.platform)
        name = PLATFORM_NAMES.get(qs.platform, qs.platform)
        try:
            state = await acc.qr_poll(qs.session)
        except AccountError as e:
            self._qr.pop(sid, None)
            logger.info("[一起听] {}扫码登录失败：{}", name, e)
            raise LibraryError(str(e)) from e
        except Exception as e:
            self._qr.pop(sid, None)
            logger.opt(exception=e).warning("[一起听] {}扫码登录出错", name)
            raise LibraryError(f"{name}登录出错（{type(e).__name__}），详情见 NoneBot 日志") from e
        if state.status != "done" or not state.credential:
            if state.status in ("expired", "refused"):
                self._qr.pop(sid, None)
            return {"status": state.status, "message": state.message}
        try:
            info = await acc.account(state.credential)
        except AccountError as e:
            self._qr.pop(sid, None)
            logger.info("[一起听] {}扫码成功，但读不到账号信息：{}", name, e)
            raise LibraryError(f"登录成功了，但读不到账号信息：{e}") from e
        async with self._lock(owner, qs.platform):
            self._save(owner, qs.platform, state.credential, info)
        qs.status = "done"
        if owner != BOT:
            self.store.link_account(owner, qs.platform, info.account_id, info.name)
            try:
                qs.playlists = await acc.playlists(credential=state.credential)
            except AccountError as e:
                return {"status": "done", "playlists": [], "message": f"登录好了，但歌单读取失败：{e}",
                        "account": info.name}
        return {"status": "done", "account": info.name, "vip": info.vip,
                "playlists": [_remote_dict(p) for p in qs.playlists]}

    # ------------------------------------------------------------ 导入

    async def list_by_uid(self, owner: str, platform: str, account_id: str) -> list[dict[str, Any]]:
        account_id = account_id.strip()
        if not account_id.isdigit() or len(account_id) > 20:
            raise LibraryError("用户 ID 应该是一串数字")
        acc = self._account(platform)
        try:
            found = await acc.playlists(account_id=account_id)
        except AccountError as e:
            raise LibraryError(str(e)) from e
        self.store.link_account(owner, platform, account_id, "")
        return [_remote_dict(p) for p in found if not p.private]

    async def import_playlists(self, owner: str, platform: str, remote_ids: list[str], *,
                               via: str) -> list[dict[str, Any]]:
        """导入勾选的歌单。via=login 时用这个人的凭证读（能读私密歌单）；via=uid 时只读公开的。"""
        acc = self._account(platform)
        if len(remote_ids) > 50:
            raise LibraryError("一次最多导入 50 个歌单")
        cred = None
        if via == "login":
            loaded = self._load(owner, platform)
            if loaded is None or loaded[1].status != "ok":
                raise LibraryError("登录已经失效了，重新扫码")
            cred = loaded[0]
        account_id = self._account_id(owner, platform)
        try:
            available = {p.remote_id: p for p in await acc.playlists(credential=cred, account_id=account_id)}
        except CredentialExpired as e:
            self.store.mark_credential(owner, platform, "expired", str(e))
            raise LibraryError(str(e)) from e
        except AccountError as e:
            raise LibraryError(str(e)) from e
        imported = []
        for rid in remote_ids:
            remote = available.get(str(rid))
            if remote is None or (via == "uid" and remote.private):
                continue
            imported.append(await self._sync_one(owner, platform, remote, cred, via=via, new=True))
        return imported

    def _account_id(self, owner: str, platform: str) -> str:
        row = self.store.credential(owner, platform)
        if row and row.account_id:
            return row.account_id
        linked = self.store.linked_account(owner, platform)
        return linked[0] if linked else ""

    async def _sync_one(self, owner: str, platform: str, remote: RemotePlaylist, cred: dict[str, Any] | None, *,
                        via: str, new: bool = False) -> dict[str, Any]:
        tracks = await self._account(platform).tracks(remote, credential=cred, limit=MAX_PLAYLIST_TRACKS)
        name = remote.name
        pid = self.store.upsert_playlist(
            owner, platform, remote.remote_id,
            name=name, cover=remote.cover, tracks=[t.to_dict() for t in tracks],
            track_count=remote.track_count, via=via,
            # 新导入时：平台上的私密歌单默认仅自己可见；同步时保留用户自己的设置
            hidden=remote.private if new else None,
            now=self._clock(),
        )
        return {"id": pid, "name": name, "tracks": len(tracks), "hidden": remote.private if new else None}

    # ------------------------------------------------------------ 每日同步

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for acc in self.accounts.values():
            close = getattr(acc, "close", None)
            if close:
                with contextlib.suppress(Exception):
                    await close()

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.run_daily()
            except Exception as e:  # noqa: BLE001 后台任务不能因为一个平台出错就停掉
                logger.opt(exception=e).warning("[一起听] 每日同步出错")
            await asyncio.sleep(3600)

    async def run_daily(self) -> dict[str, int]:
        """刷新凭证（12 小时一次）、同步歌单（24 小时一次）。请求之间间隔几秒，避免触发平台风控。"""
        stats = {"refreshed": 0, "expired": 0, "synced": 0, "failed": 0}
        now = self._clock()
        for owner, platform, row in self.store.credentials():
            if row.status != "ok" or now - row.refreshed_at < REFRESH_INTERVAL_MS or platform not in self.accounts:
                continue
            async with self._lock(owner, platform):
                loaded = self._load(owner, platform)
                if loaded is None:
                    continue
                acc = self.accounts[platform]
                try:
                    fresh = await acc.refresh(loaded[0])
                    info = await acc.account(fresh)
                except CredentialExpired as e:
                    self.store.mark_credential(owner, platform, "expired", str(e))
                    if owner == BOT:
                        self._bot_cache.pop(platform, None)
                        self.on_bot_credential_changed(platform)
                    stats["expired"] += 1
                    continue
                except AccountError as e:
                    logger.info("[一起听] 刷新 {} 的{}登录失败：{}", owner, PLATFORM_NAMES.get(platform), e)
                    continue
                self._save(owner, platform, fresh, info)
                stats["refreshed"] += 1
            await asyncio.sleep(2)

        for pid, owner, platform, remote_id, via in self.store.playlists_to_sync(now - SYNC_INTERVAL_MS):
            if platform not in self.accounts:
                continue
            cred = None
            if via == "login":
                loaded = self._load(owner, platform)
                cred = loaded[0] if loaded and loaded[1].status == "ok" else None
                if cred is None:
                    self.store.mark_playlist_error(pid, "登录失效了，重新扫码才能继续同步")
                    stats["failed"] += 1
                    continue
            try:
                remotes = await self.accounts[platform].playlists(
                    credential=cred, account_id=self._account_id(owner, platform)
                )
                remote = next((p for p in remotes if p.remote_id == remote_id), None)
                if remote is None:
                    self.store.mark_playlist_error(pid, "平台上找不到这个歌单了（删了，或者设成了私密）")
                    stats["failed"] += 1
                    continue
                await self._sync_one(owner, platform, remote, cred, via=via)
                stats["synced"] += 1
            except CredentialExpired as e:
                self.store.mark_credential(owner, platform, "expired", str(e))
                self.store.mark_playlist_error(pid, str(e))
                stats["failed"] += 1
            except AccountError as e:
                self.store.mark_playlist_error(pid, str(e))
                stats["failed"] += 1
            await asyncio.sleep(3)
        return stats

    # ------------------------------------------------------------ 喜欢的音乐、查看

    def like(self, qq: str, track: Track) -> bool:
        try:
            return self.store.add_favorite(qq, track.ref, track.to_dict(), self._clock())
        except ValueError as e:
            raise LibraryError(str(e)) from e

    def unlike(self, qq: str, ref: str) -> None:
        self.store.remove_favorite(qq, ref)

    def liked_refs(self, qq: str) -> set[str]:
        return self.store.favorite_refs(qq)

    def overview(self, qq: str, *, viewer: str) -> dict[str, Any]:
        """资料卡里的曲库。别人只看得到“可见”的歌单。"""
        mine = viewer == qq
        favorites = self.store.favorites(qq)
        return {
            "favorites": {"count": len(favorites), "tracks": favorites[:200]},
            "playlists": self.store.playlists(qq, include_hidden=mine),
        }

    def playlist(self, playlist_id: int, *, viewer: str) -> dict[str, Any] | None:
        data = self.store.playlist(playlist_id)
        if data is None or (data["hidden"] and data["qq"] != viewer):
            return None
        return data

    def summary_for_recommend(self, qq: str, limit: int = 30) -> list[str]:
        """凛音推荐歌时参考的歌单摘要：只给提问人自己的，取最近喜欢的和各歌单前几首。"""
        names: list[str] = []
        for t in self.store.favorites(qq)[:limit]:
            names.append(f"{t.get('title')} - {t.get('artists')}")
        for p in self.store.playlists(qq, include_hidden=True):
            if len(names) >= limit:
                break
            data = self.store.playlist(int(p["id"]))
            for t in (data or {}).get("tracks", [])[:5]:
                names.append(f"{t.get('title')} - {t.get('artists')}")
        return names[:limit]


def _remote_dict(p: RemotePlaylist) -> dict[str, Any]:
    return {"remote_id": p.remote_id, "name": p.name, "track_count": p.track_count, "cover": p.cover,
            "private": p.private, "liked": p.liked}
