"""bot 的会员账号：WebUI 里扫码登录三个平台，凭证加密保存，每 12 小时刷新一次。音源取会员歌的地址时用。

凭证的流向：扫码成功 → Vault 加密 → SQLite。要用时解密到内存，不写日志、不返回给 WebUI。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nonebot import logger

from .accounts.base import AccountError, AccountInfo, CredentialExpired, PlatformAccounts, QrStart
from .crypto import CryptoError, Sealed, Vault
from .sources.base import PLATFORM_NAMES
from .store import CredentialRow, Store

BOT = "bot"
QR_SESSION_TTL = 300.0
REFRESH_INTERVAL_MS = 12 * 3600 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


class VipError(Exception):
    """message 直接显示给用户。"""


@dataclass
class _QrSession:
    platform: str
    session: dict[str, Any]
    created: float


class VipAccounts:
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
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        # 已解密的凭证缓存在内存里，避免每次取播放地址都解密
        self._cache: dict[str, dict[str, Any]] = {}
        self.on_changed: Callable[[str], None] = lambda platform: None

    def _lock(self, platform: str) -> asyncio.Lock:
        return self._locks.setdefault(platform, asyncio.Lock())

    def _account(self, platform: str) -> PlatformAccounts:
        acc = self.accounts.get(platform)
        if acc is None:
            raise VipError(f"{PLATFORM_NAMES.get(platform, platform)}没有接入")
        return acc

    def login_kinds(self, platform: str) -> list[str]:
        acc = self.accounts.get(platform)
        return list(acc.login_kinds) if acc and self.vault else []

    # ------------------------------------------------------------ 凭证

    def _save(self, platform: str, cred: dict[str, Any], info: AccountInfo) -> None:
        if self.vault is None:
            raise VipError("没有配置 MUSIC_CREDENTIAL_KEY，不能保存登录")
        sealed = self.vault.seal(BOT, platform, cred)
        self.store.save_credential(BOT, platform, CredentialRow(
            sealed.ciphertext, sealed.nonce, sealed.key_id, info.account_id, info.name, info.vip,
        ), self._clock())
        self._cache[platform] = cred
        self.on_changed(platform)

    def _load(self, platform: str) -> tuple[dict[str, Any], CredentialRow] | None:
        if self.vault is None:
            return None
        row = self.store.credential(BOT, platform)
        if row is None:
            return None
        try:
            cred = self.vault.open(BOT, platform, Sealed(row.ciphertext, row.nonce, row.key_id))
        except CryptoError as e:
            self.store.mark_credential(BOT, platform, "expired", str(e))
            return None
        if self.vault.needs_rotation(Sealed(row.ciphertext, row.nonce, row.key_id)):
            sealed = self.vault.seal(BOT, platform, cred)
            self.store.save_credential(BOT, platform, CredentialRow(
                sealed.ciphertext, sealed.nonce, sealed.key_id, row.account_id, row.account_name, row.vip,
            ), row.refreshed_at)
        return cred, row

    def credential(self, platform: str) -> dict[str, Any] | None:
        """取播放地址时用。只返回状态正常的。"""
        if platform in self._cache:
            return self._cache[platform]
        loaded = self._load(platform)
        if loaded is None or loaded[1].status != "ok":
            return None
        self._cache[platform] = loaded[0]
        return loaded[0]

    def credential_failed(self, platform: str, message: str) -> None:
        """取播放地址时平台说登录失效了：标记失效，WebUI 上提示重新扫码。"""
        self._cache.pop(platform, None)
        self.store.mark_credential(BOT, platform, "expired", message)
        self.on_changed(platform)

    def status(self) -> list[dict[str, Any]]:
        out = []
        for platform in self.accounts:
            row = self.store.credential(BOT, platform)
            out.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "logged_in": row is not None and row.status == "ok",
                "expired": row is not None and row.status != "ok",
                "message": row.status_message if row else "",
                "account_name": row.account_name if row else "",
                "vip": bool(row and row.vip),
                "refreshed_at": row.refreshed_at if row else 0,
                "login_kinds": self.login_kinds(platform),
            })
        return out

    def logout(self, platform: str) -> None:
        self.store.delete_credential(BOT, platform)
        self._cache.pop(platform, None)
        self.on_changed(platform)

    # ------------------------------------------------------------ 扫码

    def _purge_qr(self) -> None:
        now = self._monotonic()
        self._qr = {k: v for k, v in self._qr.items() if now - v.created < QR_SESSION_TTL}

    async def qr_start(self, platform: str, kind: str) -> tuple[str, QrStart]:
        if self.vault is None:
            raise VipError("扫码登录没有开启：还没配置 MUSIC_CREDENTIAL_KEY")
        acc = self._account(platform)
        if kind not in acc.login_kinds:
            kind = acc.login_kinds[0]
        self._purge_qr()
        # 同一时间只保留一个二维码
        self._qr.clear()
        try:
            start = await acc.qr_start(kind)
        except AccountError as e:
            raise VipError(str(e)) from e
        except Exception as e:
            logger.opt(exception=e).warning("[点歌] {}二维码生成出错", PLATFORM_NAMES.get(platform, platform))
            raise VipError(f"二维码生成失败（{type(e).__name__}），详情见 NoneBot 日志") from e
        sid = secrets.token_urlsafe(18)
        self._qr[sid] = _QrSession(platform, start.session, self._monotonic())
        return sid, start

    async def qr_poll(self, sid: str) -> dict[str, Any]:
        """返回 {status, message, account?, vip?}。扫码成功时保存凭证。"""
        self._purge_qr()
        qs = self._qr.get(sid)
        if qs is None:
            return {"status": "expired", "message": "二维码过期了，重新获取一个"}
        acc = self._account(qs.platform)
        name = PLATFORM_NAMES.get(qs.platform, qs.platform)
        try:
            state = await acc.qr_poll(qs.session)
        except AccountError as e:
            self._qr.pop(sid, None)
            logger.info("[点歌] {}扫码登录失败：{}", name, e)
            raise VipError(str(e)) from e
        except Exception as e:
            self._qr.pop(sid, None)
            logger.opt(exception=e).warning("[点歌] {}扫码登录出错", name)
            raise VipError(f"{name}登录出错（{type(e).__name__}），详情见 NoneBot 日志") from e
        if state.status != "done" or not state.credential:
            if state.status in ("expired", "refused"):
                self._qr.pop(sid, None)
            return {"status": state.status, "message": state.message}
        try:
            info = await acc.account(state.credential)
        except AccountError as e:
            self._qr.pop(sid, None)
            logger.info("[点歌] {}扫码成功，但读不到账号信息：{}", name, e)
            raise VipError(f"登录成功了，但读不到账号信息：{e}") from e
        async with self._lock(qs.platform):
            self._save(qs.platform, state.credential, info)
        self._qr.pop(sid, None)
        return {"status": "done", "account": info.name, "vip": info.vip}

    # ------------------------------------------------------------ 定时刷新

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
                await self.refresh_due()
            except Exception as e:  # noqa: BLE001 后台任务不能因为一个平台出错就停掉
                logger.opt(exception=e).warning("[点歌] 刷新会员账号出错")
            await asyncio.sleep(3600)

    async def refresh_due(self) -> dict[str, int]:
        """刷新超过 12 小时没刷新的凭证。请求之间隔几秒，避免触发平台风控。"""
        stats = {"refreshed": 0, "expired": 0}
        now = self._clock()
        for platform, row in self.store.credentials(BOT):
            if row.status != "ok" or now - row.refreshed_at < REFRESH_INTERVAL_MS or platform not in self.accounts:
                continue
            async with self._lock(platform):
                loaded = self._load(platform)
                if loaded is None:
                    continue
                acc = self.accounts[platform]
                try:
                    fresh = await acc.refresh(loaded[0])
                    info = await acc.account(fresh)
                except CredentialExpired as e:
                    self.store.mark_credential(BOT, platform, "expired", str(e))
                    self._cache.pop(platform, None)
                    self.on_changed(platform)
                    stats["expired"] += 1
                    continue
                except AccountError as e:
                    logger.info("[点歌] 刷新{}会员账号失败：{}", PLATFORM_NAMES.get(platform), e)
                    continue
                self._save(platform, fresh, info)
                stats["refreshed"] += 1
            await asyncio.sleep(2)
        return stats
