"""Pixiv 登录 Cookie 的整理和迁移。不依赖 NoneBot。"""

from __future__ import annotations

import re
from typing import Optional

_PREFIX = re.compile(r"^cookie\s*:\s*", re.IGNORECASE)
_SESSION_USER = re.compile(r"(?:^|;\s*)PHPSESSID=(\d+)_")


def normalize_cookie(raw: str) -> str:
    """整理用户粘贴的 Cookie。只粘了 PHPSESSID 的值（12345678_xxx）时补上名字；去掉换行，防止改写请求头。"""
    text = " ".join(str(raw or "").split())
    text = _PREFIX.sub("", text).strip().strip(";").strip()
    if not text:
        return ""
    return text if "=" in text else f"PHPSESSID={text}"


def cookie_user_id(cookie: Optional[str]) -> Optional[str]:
    """PHPSESSID 的格式是「账号ID_随机串」，前半段就是 Cookie 对应的 Pixiv 账号。"""
    m = _SESSION_USER.search(cookie or "")
    return m.group(1) if m else None


def migrate_legacy_cookie(storage, store, section_id: str = "pixiv", key: str = "cookie") -> bool:
    """原来用 /p站 登录 存在 SQLite 里的 Cookie 搬到 WebUI 的配置里，只搬一次。

    WebUI 里已经有值（或者来自环境变量）时不动。搬完删掉 SQLite 里那份，免得在 WebUI 里清空后又被搬回来。
    """
    if str(store.get(section_id, key) or "").strip():
        return False
    data = storage.load_cookie()
    cookie = normalize_cookie((data or {}).get("full_cookie") or "")
    if not cookie:
        return False
    if store.update(section_id, {key: cookie}):
        return False
    storage.delete_cookie()
    return True
