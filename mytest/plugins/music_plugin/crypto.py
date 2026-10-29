"""平台登录凭证的加密保存（AES-256-GCM）。

MUSIC_CREDENTIAL_KEY=32 字节的 base64         # 生成：python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
MUSIC_CREDENTIAL_KEY_OLD=上一把密钥            # 换密钥时临时填，后台会把旧密文逐条重新加密

- 每条凭证用随机 12 字节 nonce。
- 附加数据（AAD）是 owner:platform:key_id：把别人的密文挪到自己名下、或者换到别的平台，解密都会失败。
- 密钥只在环境变量里，不进数据库、不进日志。key_id 是密钥 SHA-256 的前 8 位十六进制，只用来区分是哪把钥匙加密的。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class CryptoError(Exception):
    """密钥不对、密文被改过、或者没有配置密钥。"""


def parse_key(text: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise CryptoError("MUSIC_CREDENTIAL_KEY 不是合法的 base64") from None
    if len(key) != 32:
        raise CryptoError(f"MUSIC_CREDENTIAL_KEY 解码后是 {len(key)} 字节，需要 32 字节")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"listen-credential-key:" + key).hexdigest()[:8]


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes
    key_id: str


class Vault:
    def __init__(self, key: bytes, old_key: bytes | None = None):
        self._keys = {key_id(key): AESGCM(key)}
        self.current = key_id(key)
        if old_key is not None and key_id(old_key) != self.current:
            self._keys[key_id(old_key)] = AESGCM(old_key)

    @classmethod
    def from_env(cls, current: str, old: str = "") -> Vault | None:
        """没配置密钥时返回 None（扫码导入和 bot 会员账号不可用，其他功能照常）。"""
        if not current.strip():
            return None
        return cls(parse_key(current), parse_key(old) if old.strip() else None)

    @staticmethod
    def _aad(owner: str, platform: str, kid: str) -> bytes:
        return f"{owner}:{platform}:{kid}".encode()

    def seal(self, owner: str, platform: str, data: dict[str, Any]) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        plain = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        ct = self._keys[self.current].encrypt(nonce, plain, self._aad(owner, platform, self.current))
        return Sealed(ct, nonce, self.current)

    def open(self, owner: str, platform: str, sealed: Sealed) -> dict[str, Any]:
        aes = self._keys.get(sealed.key_id)
        if aes is None:
            raise CryptoError("加密这条凭证的密钥已经不在了，需要重新扫码")
        try:
            plain = aes.decrypt(sealed.nonce, sealed.ciphertext, self._aad(owner, platform, sealed.key_id))
        except InvalidTag:
            raise CryptoError("凭证解密失败（密文被改过，或者不属于这个账号）") from None
        data = json.loads(plain)
        if not isinstance(data, dict):
            raise CryptoError("凭证格式不对")
        return data

    def needs_rotation(self, sealed: Sealed) -> bool:
        return sealed.key_id != self.current
