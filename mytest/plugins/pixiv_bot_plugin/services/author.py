from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..parser import convert_mode_to_english
from .image_selection import build_images_from_candidates, select_quality_random_images


@dataclass
class AuthorImagesResult:
    ok: bool
    message: str
    images: List[dict] | None = None
    title: str = ""
    mode: str = ""
    count: int = 0


@dataclass
class AuthorInfoResult:
    ok: bool
    message: str
    resolved_user_id: str = ""
    original_input: str = ""
    author_info: Optional[Dict[str, Any]] = None
    used_alias: bool = False


async def get_author_images(spider, user_id: str, mode: str, count: int) -> AuthorImagesResult:
    if not user_id:
        return AuthorImagesResult(False, "请指定作者ID或圈名，例如：p站 作者 123456 3 随机")

    english_mode = convert_mode_to_english(mode or "")
    if english_mode not in ["random", "recent", "popular"]:
        return AuthorImagesResult(False, "模式必须是：随机、最新、热门（或：random、recent、popular）")

    count = min(max(count or 1, 1), 5)
    author_result = await _get_author_images(spider, user_id, english_mode, count)
    if not author_result:
        return AuthorImagesResult(False, f"未找到作者「{user_id}」或其作品", mode=english_mode, count=count)

    images = author_result.get("images", [])
    author_info = author_result.get("author_info", {})
    if not images:
        return AuthorImagesResult(
            False,
            f"作者「{author_info.get('name', user_id)}」没有符合条件的作品",
            mode=english_mode,
            count=count,
        )

    author_name = author_info.get("name", "未知作者")
    favorite_text = "（已关注）" if author_info.get("is_favorite", False) else ""
    return AuthorImagesResult(
        True,
        "",
        images=images,
        title=f"作者作品「{author_name}」{favorite_text}",
        mode=english_mode,
        count=count,
    )


async def _get_author_images(spider, user_id: str, mode: str, count: int) -> Optional[Dict[str, Any]]:
    if not user_id or not str(user_id).strip():
        return None
    if count < 1 or count > 5:
        return None
    if mode not in ["random", "recent", "popular"]:
        return None

    original_input = str(user_id).strip()
    resolved_user_id = spider.search_author_by_alias(original_input) or original_input
    is_favorite = resolved_user_id in spider.favorite_authors
    favorite_info = spider.favorite_authors.get(resolved_user_id, {})
    author_name = favorite_info.get("name", "未知作者")

    author_data = await spider.get_author_profile(resolved_user_id)
    if not author_data:
        return None

    illusts_dict = author_data.get("illusts", {})
    illusts = list(illusts_dict.values()) if isinstance(illusts_dict, dict) else []
    if not illusts:
        return None

    if mode == "random":
        images = await select_quality_random_images(
            spider,
            illusts,
            count,
            min_quality_score=60.0,
            max_attempts=10,
            allow_fallback=True,
            source_prefix="author",
        )
    elif mode == "recent":
        sorted_illusts = sorted(illusts, key=lambda item: item.get("createDate", ""), reverse=True)
        images = await build_images_from_candidates(spider, sorted_illusts, count, "author_recent")
    else:
        sorted_illusts = sorted(illusts, key=lambda item: int(item.get("bookmarkCount", 0) or 0), reverse=True)
        images = await build_images_from_candidates(spider, sorted_illusts, count, "author_popular")

    if not images:
        return None

    if is_favorite:
        spider.update_author_last_check(resolved_user_id)

    return {
        "author_info": {
            "user_id": resolved_user_id,
            "name": author_name,
            "is_favorite": is_favorite,
            "total_works": len(illusts),
            "profile_image_url": author_data.get("image", ""),
            "comment": author_data.get("comment", ""),
            "followable": author_data.get("followable", False),
        },
        "search_info": {
            "mode": mode,
            "requested_count": count,
            "actual_count": len(images),
        },
        "images": images,
    }


def follow_author(spider, user_id: str, author_name: str) -> str:
    if not user_id:
        return "请指定作者ID，例如：p站 关注 123456 画师名"

    if spider.add_favorite_author(user_id, author_name or ""):
        name_text = f"「{author_name}」" if author_name else ""
        return f"✅ 已成功关注作者 {name_text}（{user_id}）"
    return "❌ 关注作者失败，可能已经关注过"


def unfollow_author(spider, user_id: str) -> str:
    if not user_id:
        return "请指定作者ID，例如：p站 取关 123456"

    author_info = spider.get_favorite_author_info(user_id)
    if not author_info:
        return f"❌ 作者 {user_id} 不在关注列表中"

    author_name = author_info.get("name", "未知作者")
    if spider.remove_favorite_author(user_id):
        return f"✅ 已成功取关作者「{author_name}」（{user_id}）"
    return "❌ 取关作者失败"


def build_follow_list_message(spider) -> str:
    authors_info = spider.get_favorite_authors_info()
    total_count = authors_info.get("total_count", 0)
    if total_count == 0:
        return "你还没有关注任何作者"

    message = f"📋 关注列表（共{total_count}个作者）\n\n"
    message += f"🆕 最近7天添加：{authors_info.get('recent_added', 0)}个\n"
    message += f"👀 从未检查：{authors_info.get('never_checked', 0)}个\n\n"

    for index, (user_id, author_info) in enumerate(authors_info.get("authors", {}).items()):
        if index >= 10:
            break
        name = author_info.get("name", "未知")
        aliases = author_info.get("aliases", [])
        alias_text = f"（圈名：{', '.join(aliases)}）" if aliases else ""
        message += f"• {name}（{user_id}）{alias_text}\n"

    if total_count > 10:
        message += f"\n... 还有{total_count - 10}个作者"
    return message


async def get_author_info(spider, user_id: str) -> AuthorInfoResult:
    if not user_id:
        return AuthorInfoResult(False, "请指定作者ID或圈名，例如：p站 作者信息 12345678")

    original_input = str(user_id).strip()
    resolved_user_id = spider.search_author_by_alias(original_input) or original_input
    author_info = await spider.get_author_info(resolved_user_id)
    if not author_info:
        return AuthorInfoResult(False, f"未找到「{original_input}」对应的作者", resolved_user_id, original_input)

    return AuthorInfoResult(
        True,
        "",
        resolved_user_id=resolved_user_id,
        original_input=original_input,
        author_info=author_info,
        used_alias=resolved_user_id != original_input,
    )


def author_info_progress_message(original_input: str, resolved_user_id: str) -> str:
    if resolved_user_id != original_input:
        return f"正在获取作者信息（圈名: {original_input} -> ID: {resolved_user_id}），请稍候..."
    return f"正在获取作者信息（ID: {resolved_user_id}），请稍候..."


def build_author_info_message(result: AuthorInfoResult) -> str:
    author_info = result.author_info or {}
    message = "👤 作者详细信息\n\n"
    message += f"名称: {author_info.get('name', '未知作者')}\n"
    message += f"ID: {author_info.get('userId', result.resolved_user_id)}\n"
    if result.used_alias:
        message += f"圈名: {result.original_input}\n"
    message += f"作品总数: {author_info.get('totalWorks', 0)}\n"
    message += f"可关注: {'是' if author_info.get('followable', False) else '否'}\n"
    message += f"接受请求: {'是' if author_info.get('acceptRequest', False) else '否'}\n"
    message += f"已关注: {'是' if author_info.get('isFollowed', False) else '否'}\n"
    message += f"高级会员: {'是' if author_info.get('isPremium', False) else '否'}\n"
    message += f"MyPixiv: {'是' if author_info.get('isMypixiv', False) else '否'}\n"

    comment = author_info.get("comment", "").strip()
    if comment:
        if len(comment) > 300:
            comment = comment[:300] + "..."
        message += f"\n\n简介: {comment}"
    return message


def build_latest_images_message(author_info: Dict[str, Any]) -> str:
    latest_images = author_info.get("latestImages", [])
    message = f"📸 最新作品（{len(latest_images)}张）:\n"
    for i, img in enumerate(latest_images[:3], 1):
        img_title = img.get("title", "无标题")
        img_id = img.get("id", "")
        img_date = img.get("createDate", "")
        if img_date:
            img_date = img_date.split("T")[0]
        message += f"\n{i}. {img_title[:20]}... (ID: {img_id}, {img_date})"

    if len(latest_images) > 3:
        message += f"\n... 还有{len(latest_images) - 3}张作品"
    return message
