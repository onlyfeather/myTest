"""关注作者更新推送：找出作者上次检查之后的新作品。不依赖 NoneBot，发送由调用方做。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .image_selection import build_image_info


@dataclass
class AuthorUpdate:
    user_id: str
    name: str
    images: list[dict[str, Any]] = field(default_factory=list)  # 要发的作品，从旧到新
    skipped: int = 0  # 超出每轮上限、没有发图的新作品数
    first_check: bool = False  # 第一次检查：只记下最新作品，不推送


def _illust_id(illust: dict[str, Any]) -> int:
    try:
        return int(illust.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _blocked(spider, illust: dict[str, Any], *, allow_r18: bool) -> bool:
    return spider.is_blocked(illust, allow_r18=allow_r18)


async def check_author(spider, user_id: str, *, max_images: int, allow_r18: bool) -> AuthorUpdate | None:
    """返回这个作者的更新；取不到作者数据时返回 None（不更新记录，下一轮再试）。"""
    info = spider.favorite_authors.get(user_id, {})
    name = str(info.get("name") or "") or user_id
    profile = await spider.get_author_profile(user_id)
    if not profile:
        return None
    raw = profile.get("illusts") or {}
    illusts = [i for i in (raw.values() if isinstance(raw, dict) else raw) if isinstance(i, dict) and _illust_id(i)]
    if not illusts:
        return AuthorUpdate(user_id, name)
    illusts.sort(key=_illust_id)
    newest = _illust_id(illusts[-1])
    if not name or name == user_id:
        name = str(illusts[-1].get("userName") or name)

    seen = spider.storage.get_author_seen(user_id)
    if seen is None:
        spider.storage.set_author_seen(user_id, newest)
        return AuthorUpdate(user_id, name, first_check=True)

    fresh = [i for i in illusts if _illust_id(i) > seen]
    wanted = [i for i in fresh if not _blocked(spider, i, allow_r18=allow_r18)]
    # 只发最新的几张，按发布顺序从旧到新
    shown = wanted[-max_images:]
    images = [await build_image_info(spider, i, "follow_push") for i in shown]
    if newest > seen:
        spider.storage.set_author_seen(user_id, newest)
    return AuthorUpdate(user_id, name, images, skipped=len(wanted) - len(shown))


def update_title(update: AuthorUpdate) -> str:
    return f"🔔 关注的作者「{update.name}」更新了"


def skipped_note(update: AuthorUpdate) -> str:
    return f"「{update.name}」还有 {update.skipped} 张新作品没有发：p站 作者 {update.user_id} 3 最新"
