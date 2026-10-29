"""身份：一次性登录链接（方案 A）、验证码（方案 B）、会话。

不依赖 NoneBot。会话和已用过的链接存 SQLite（见 store.py），验证码只放内存（5 分钟有效，重启作废无所谓）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

LINK_TTL_MS = 10 * 60 * 1000
CODE_TTL_MS = 5 * 60 * 1000
SESSION_TTL_MS = 30 * 24 * 3600 * 1000
CODE_ATTEMPTS_PER_MINUTE = 5


class AuthError(Exception):
    """登录失败。message 直接显示给用户。"""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    qq: str
    group_id: str


class LinkSigner:
    """登录链接：HMAC-SHA256 签名的 {qq, group_id, exp, jti}。"""

    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("LISTEN_SECRET 至少 32 位")
        self._key = hashlib.sha256(b"listen-login:" + secret.encode()).digest()

    def issue(self, qq: str, group_id: str, now: int) -> str:
        payload = json.dumps(
            {"qq": qq, "g": group_id, "exp": now + LINK_TTL_MS, "jti": secrets.token_urlsafe(12)},
            separators=(",", ":"),
        ).encode()
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify(self, token: str, now: int) -> tuple[Identity, str, int]:
        """返回 (身份, jti, 过期时间)。签名不对或过期时抛 AuthError。jti 是否用过由调用方检查。"""
        try:
            body, sig = token.split(".", 1)
            payload = _unb64(body)
            given = _unb64(sig)
        except ValueError:
            raise AuthError("登录链接不完整") from None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(given, expected):
            raise AuthError("登录链接无效")
        try:
            data = json.loads(payload)
            qq, group_id, exp, jti = str(data["qq"]), str(data["g"]), int(data["exp"]), str(data["jti"])
        except (ValueError, KeyError, TypeError):
            raise AuthError("登录链接无效") from None
        if now > exp:
            raise AuthError("登录链接过期了，在群里重新发 /一起听")
        return Identity(qq, group_id), jti, exp


@dataclass
class PendingCode:
    code: str
    browser: str  # 发起验证码的浏览器（随机 ID），登录成功后只通知这个浏览器
    expires: int
    result: Identity | None = None


class CodeBook:
    """验证码：页面拿一个 6 位码，用户把码发给凛音，bot 告诉我们是谁发的。"""

    def __init__(self) -> None:
        self._by_code: dict[str, PendingCode] = {}
        self._by_browser: dict[str, str] = {}
        self._attempts: dict[str, deque[int]] = {}

    def _purge(self, now: int) -> None:
        for code, pending in list(self._by_code.items()):
            if now > pending.expires:
                self._by_code.pop(code, None)
                if self._by_browser.get(pending.browser) == code:
                    self._by_browser.pop(pending.browser, None)

    def create(self, browser: str, now: int) -> str:
        self._purge(now)
        old = self._by_browser.pop(browser, None)
        if old:
            self._by_code.pop(old, None)
        if len(self._by_code) > 10_000:
            raise AuthError("登录的人太多了，稍后再试")
        while True:
            code = f"{secrets.randbelow(1_000_000):06d}"
            if code not in self._by_code:
                break
        self._by_code[code] = PendingCode(code, browser, now + CODE_TTL_MS)
        self._by_browser[browser] = code
        return code

    def redeem(self, code: str, identity: Identity, now: int) -> bool:
        """bot 收到用户发来的码时调用。同一个 QQ 每分钟最多试 5 次。返回是否成功。"""
        recent = self._attempts.setdefault(identity.qq, deque())
        while recent and now - recent[0] >= 60_000:
            recent.popleft()
        if len(recent) >= CODE_ATTEMPTS_PER_MINUTE:
            raise AuthError("试得太频繁了，一分钟后再试")
        recent.append(now)
        self._purge(now)
        pending = self._by_code.get(code.strip())
        if pending is None or pending.result is not None:
            return False
        pending.result = identity
        return True

    def poll(self, browser: str, now: int) -> Identity | None:
        """页面轮询：码被兑换后返回身份，并作废这个码。"""
        self._purge(now)
        code = self._by_browser.get(browser)
        pending = self._by_code.get(code) if code else None
        if pending is None or pending.result is None:
            return None
        self._by_code.pop(pending.code, None)
        self._by_browser.pop(browser, None)
        return pending.result

    def active(self, browser: str, now: int) -> bool:
        self._purge(now)
        return browser in self._by_browser


class Sessions:
    """会话 cookie。数据库只存会话 ID 的 SHA-256。"""

    def __init__(self, store: Store):
        self._store = store

    def create(self, identity: Identity, now: int) -> str:
        token = secrets.token_urlsafe(32)
        self._store.add_session(hash_token(token), identity.qq, identity.group_id, now + SESSION_TTL_MS)
        return token

    def get(self, token: str | None, now: int) -> Identity | None:
        if not token or len(token) > 100:
            return None
        found = self._store.get_session(hash_token(token), now)
        return Identity(*found) if found else None

    def revoke(self, token: str | None) -> None:
        if token:
            self._store.delete_session(hash_token(token))

    def revoke_user(self, qq: str, group_id: str | None = None) -> None:
        self._store.delete_sessions_of(qq, group_id)

    def revoke_group(self, group_id: str) -> None:
        self._store.delete_sessions_in(group_id)

    def consume_link(self, signer: LinkSigner, token: str, now: int) -> Identity:
        identity, jti, exp = signer.verify(token, now)
        if not self._store.use_link(jti, exp, now):
            raise AuthError("这个登录链接已经用过了，在群里重新发 /一起听")
        return identity
