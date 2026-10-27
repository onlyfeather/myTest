"""从 QQ 消息里取图片：消息本身带的图，以及被回复的那条消息里的图。"""

from __future__ import annotations

from typing import Any

import httpx

from .services.http import ServiceError

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# 只从 QQ 的图片服务器下载，防止有人构造消息让机器人去请求内网地址。
ALLOWED_HOST_SUFFIXES = (
    ".qq.com",
    ".qpic.cn",
    ".qq.com.cn",
    ".gtimg.cn",
    ".myqcloud.com",
)


def image_urls(message: Any) -> list[str]:
    """从 OneBot 消息段里取出图片地址（NapCat 的 image 段带 url 字段）。"""
    urls = []
    for seg in message or []:
        seg_type = getattr(seg, "type", None) if not isinstance(seg, dict) else seg.get("type")
        data = getattr(seg, "data", None) if not isinstance(seg, dict) else seg.get("data")
        if seg_type != "image" or not isinstance(data, dict):
            continue
        url = str(data.get("url") or data.get("file") or "").strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def event_image_urls(event: Any) -> list[str]:
    """先取消息本身的图片，再取被回复消息里的图片。"""
    urls = image_urls(getattr(event, "message", None))
    reply = getattr(event, "reply", None)
    if reply is not None:
        urls += image_urls(getattr(reply, "message", None))
    return urls


def host_allowed(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    host = (parsed.host or "").lower()
    return parsed.scheme in {"http", "https"} and any(
        host == suffix.lstrip(".") or host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES
    )


async def download(url: str, *, timeout: float = 30.0) -> bytes:
    if not host_allowed(url):
        raise ServiceError("只能使用 QQ 里发送的图片")
    # 不跟随跳转：跳转目标可能不在白名单里。
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise ServiceError(f"图片下载失败（HTTP {resp.status_code}），图片可能已过期，请重新发送")
            declared = int(resp.headers.get("content-length") or 0)
            if declared > MAX_DOWNLOAD_BYTES:
                raise ServiceError("图片太大了，最多 20 MB")
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ServiceError("图片太大了，最多 20 MB")
                chunks.append(chunk)
    return b"".join(chunks)
