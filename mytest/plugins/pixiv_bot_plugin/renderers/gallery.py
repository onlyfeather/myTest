"""多页作品打包成 ZIP。纯计算，不依赖 NoneBot。"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse

MAX_INLINE_PAGES = 5  # 超过这么多页就打包成压缩包发群文件
MAX_ZIP_PAGES = 200
MAX_ZIP_BYTES = 200 * 1024 * 1024
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(text: str, limit: int = 60) -> str:
    """文件名里不能有 / \\ : 等字符，Windows 也要能解压。"""
    name = _UNSAFE.sub("_", str(text or "")).strip(" ._")
    return name[:limit] or "pixiv"


def page_name(index: int, url: str, total: int) -> str:
    suffix = PurePosixPath(urlparse(url).path).suffix.lower() or ".jpg"
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        suffix = ".jpg"
    return f"p{index:0{max(2, len(str(total)))}d}{suffix}"


def archive_name(illust: dict) -> str:
    title = safe_name(illust.get("title") or "")
    return f"pixiv_{illust.get('id', '')}_{title}.zip"


def build_zip(pages: list[tuple[str, bytes]]) -> bytes:
    """[(文件名, 内容)] → ZIP。图片本身已经压缩过，用 STORED 不再压一遍，省 CPU。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in pages:
            zf.writestr(name, data)
    return buffer.getvalue()
