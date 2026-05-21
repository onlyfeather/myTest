from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from nonebot import logger

from ..common import (
    BEAUTIFUL_THRESHOLDS,
    DAILY_MIN_BOOKMARKS,
    LATEST_MIN_BOOKMARKS,
    RANDOM_MIN_BOOKMARKS,
    pixiv_failure_message,
)
from .image_selection import (
    build_images_from_candidates,
    get_search_candidates,
    select_quality_random_images,
)


@dataclass
class ImageResult:
    ok: bool
    message: str
    images: List[dict] | None = None
    title: str = ""
    display_tags: str = ""
    effective_min_bookmarks: int = 0


async def search_images(spider, parsed) -> ImageResult:
    tags = parsed.search_tags
    display_tags = parsed.display_tags
    if parsed.min_bookmarks is not None:
        display_tags = f"{display_tags}（收藏≥{parsed.min_bookmarks}）"

    if parsed.illust_id:
        image = await spider.get_image_info(parsed.illust_id)
        if not image:
            return ImageResult(
                False,
                pixiv_failure_message(
                    spider,
                    "获取作品",
                    parsed.illust_id,
                    "请确认作品ID正确；也可以用 /p站 登录状态 检查 Cookie。",
                ),
            )
        return ImageResult(True, "", images=[image], title=f"作品「{parsed.illust_id}」", display_tags=display_tags)

    effective_min_bookmarks = parsed.min_bookmarks
    if parsed.mode == "random":
        effective_min_bookmarks = max(parsed.min_bookmarks or 0, RANDOM_MIN_BOOKMARKS)
        if parsed.min_bookmarks is None:
            tags = f"{tags} {RANDOM_MIN_BOOKMARKS}users入り"
            display_tags = f"{display_tags}（收藏>{RANDOM_MIN_BOOKMARKS}）"
        elif effective_min_bookmarks != parsed.min_bookmarks:
            tags = f"{parsed.display_tags} {effective_min_bookmarks}users入り"
            display_tags = f"{parsed.display_tags}（收藏>{effective_min_bookmarks}）"

    search_result = await spider.search_illustrations(tags)

    if not search_result:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "搜索",
                display_tags,
                "可先用 /p站 登录状态 检查 Cookie；如果带了收藏筛选，可以降低收藏阈值或减少标签。",
            ),
            display_tags=display_tags,
            effective_min_bookmarks=effective_min_bookmarks or 0,
        )

    images = await _select_search_images(
        spider,
        search_result,
        parsed.mode,
        parsed.count,
        min_bookmarks=effective_min_bookmarks or 0,
        max_attempts=max(20, parsed.count * 20),
    )
    if not images:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "搜索",
                display_tags,
                "可以尝试减少标签、改用随机模式，或降低收藏筛选。",
            ),
            display_tags=display_tags,
            effective_min_bookmarks=effective_min_bookmarks or 0,
        )

    return ImageResult(
        True,
        "",
        images=images,
        title=f"搜索结果「{display_tags}」",
        display_tags=display_tags,
        effective_min_bookmarks=effective_min_bookmarks or 0,
    )


async def latest_images(spider, parsed) -> ImageResult:
    search_result = await _get_latest_images(
        spider,
        parsed.tags,
        parsed.count,
        parsed.hours,
        min_bookmarks=LATEST_MIN_BOOKMARKS,
    )

    if not search_result:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "最新图片",
                parsed.tags,
                f"可以把时间范围放宽，例如：/p站 最新 {parsed.tags} {parsed.count} 168。",
            ),
        )

    images = search_result.get("images", [])
    if not images:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "最新图片",
                parsed.tags,
                f"最近 {parsed.hours} 小时内没有符合条件的作品，可以扩大到 168 小时试试。",
            ),
        )

    return ImageResult(
        True,
        "",
        images=images,
        title=f"最新图片「{parsed.tags}」（{parsed.hours}小时内，收藏>{LATEST_MIN_BOOKMARKS}）",
    )


async def beautiful_image(spider, tags: str) -> ImageResult:
    selected_threshold = 0
    images = []
    for threshold in BEAUTIFUL_THRESHOLDS:
        selected_threshold = threshold
        search_result = await spider.search_illustrations(f"{tags} {threshold}users入り")
        if not search_result:
            continue
        images = await _select_search_images(
            spider,
            search_result,
            "random",
            1,
            min_bookmarks=threshold,
            max_attempts=20,
        )
        if images:
            break

    if not images:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "美图",
                tags,
                "已按收藏>5000、>2500、>1000、>500 依次回退；仍无结果时可以减少标签或换更常见的日文/英文标签。",
            ),
        )

    return ImageResult(True, "", images=images, title=f"美图推荐「{tags}」（收藏>{selected_threshold}）")


async def hot_images(spider, tags: str, count: int) -> ImageResult:
    search_result = await spider.search_illustrations(tags)
    if not search_result:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "热门图片",
                tags,
                "热门模式依赖 Pixiv 的热门结果；如果没有返回，可以换用 搜图 或 美图。",
            ),
        )

    images = await _select_search_images(spider, search_result, "popular", count)
    if not images:
        return ImageResult(
            False,
            pixiv_failure_message(
                spider,
                "热门图片",
                tags,
                "Pixiv 没有给出可用热门作品，可以减少标签或改用随机搜图。",
            ),
        )

    return ImageResult(True, "", images=images, title=f"热门推荐「{tags}」")


async def daily_recommend(spider, user_id: str) -> ImageResult:
    daily_result = await _get_daily_recommend(spider, user_id, max_attempts=20, min_bookmarks=DAILY_MIN_BOOKMARKS)

    if not daily_result:
        return ImageResult(False, f"获取今日推荐失败：没有找到收藏>{DAILY_MIN_BOOKMARKS}的可用作品，请稍后再试或增加喜好标签。")

    image = daily_result.get("image")
    tag = daily_result.get("tag", "今日推荐")
    if not image:
        return ImageResult(False, "图片信息获取失败")

    image_id = image.get("id")
    if image_id:
        original_urls = await spider.get_illust_original_urls(image_id)
        if original_urls:
            image["urls"] = original_urls
            logger.debug(f"每日一图已获取原图: {len(original_urls)} 个URL")
        else:
            thumbnail_url = image.get("url", "")
            if thumbnail_url:
                image["urls"] = [thumbnail_url]
                logger.debug("每日一图获取原图失败，使用缩略图备用")
            else:
                logger.debug("每日一图无法获取任何图片URL")
    else:
        logger.debug("每日一图图片ID为空")

    return ImageResult(True, "", images=[image], title=f"📅 今日推荐「{tag}」")


async def _select_search_images(
    spider,
    search_result: dict,
    mode: str,
    count: int,
    *,
    min_bookmarks: int = 0,
    max_attempts: int = 10,
) -> List[dict]:
    if mode == "random":
        return await select_quality_random_images(
            spider,
            get_search_candidates(search_result),
            count,
            min_quality_score=60.0,
            max_attempts=max_attempts,
            min_bookmarks=min_bookmarks,
            allow_fallback=min_bookmarks <= 0,
            source_prefix="random",
        )
    if mode == "recent":
        return await build_images_from_candidates(
            spider,
            search_result.get("popular", {}).get("recent", []),
            count,
            "popular_recent",
            shuffle=True,
        )
    if mode == "popular":
        return await build_images_from_candidates(
            spider,
            search_result.get("popular", {}).get("permanent", []),
            count,
            "popular_permanent",
            shuffle=True,
        )
    return []


async def _get_latest_images(
    spider,
    tags: str,
    count: int,
    hours_limit: int,
    *,
    min_bookmarks: int = 0,
) -> Optional[dict]:
    if count < 1 or count > 10 or hours_limit < 1 or hours_limit > 168:
        return None

    time_threshold = datetime.now(timezone.utc) - timedelta(hours=hours_limit)
    time_threshold_str = time_threshold.strftime("%Y-%m-%dT%H:%M:%S UTC")
    search_result = await spider._search_illustrations_simple(tags, page=1)
    if not search_result:
        return None

    found_images = []
    for image in search_result.get("illusts", []):
        create_time = spider._parse_pixiv_time(image.get("createDate", ""))
        if not create_time:
            continue
        if create_time < time_threshold:
            continue

        illust_details = await spider.get_illust_details(image.get("id", ""))
        bookmark_count = int(illust_details.get("bookmarkCount", 0) or 0) if illust_details else 0
        if bookmark_count <= min_bookmarks:
            logger.debug(f"最新图片跳过低收藏作品: ID={image.get('id')}, 收藏={bookmark_count} <= {min_bookmarks}")
            continue

        hours_since_upload = (datetime.now(timezone.utc) - create_time).total_seconds() / 3600
        image["hours_since_upload"] = hours_since_upload
        image["bookmarkCount"] = bookmark_count
        found_images.append(image)
        if len(found_images) >= count:
            break

    if not found_images:
        spider.last_error_reason = "no_recent_images"
        return None

    found_images.sort(key=lambda item: item.get("createDate", ""), reverse=True)
    enhanced_images = await build_images_from_candidates(spider, found_images[:count], count, "tag_latest")
    for index, image in enumerate(enhanced_images):
        source = found_images[index]
        image["hours_since_upload"] = source.get("hours_since_upload", 0)
        image["bookmarkCount"] = source.get("bookmarkCount", 0)

    spider.last_error_reason = None
    return {
        "search_info": {
            "tags": [tags],
            "search_keyword": tags,
            "requested_count": count,
            "actual_count": len(enhanced_images),
            "hours_limit": hours_limit,
            "time_threshold": time_threshold_str,
            "total_candidates": len(found_images),
            "pages_searched": 1,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "images": enhanced_images,
        "statistics": {
            "total_found": len(found_images),
            "time_filtered": len(found_images),
            "avg_hours_since_upload": sum(img.get("hours_since_upload", 0) for img in enhanced_images)
            / len(enhanced_images)
            if enhanced_images
            else 0,
        },
    }


async def _get_daily_recommend(
    spider,
    user_id: str,
    *,
    max_attempts: int = 5,
    min_bookmarks: int = 100,
) -> Optional[dict]:
    available_tags = list(spider.preferred_tags)
    if not available_tags:
        return None

    seed = spider._get_daily_seed(user_id or "anonymous")
    start_index = seed % len(available_tags)
    ordered_tags = available_tags[start_index:] + available_tags[:start_index]
    attempted_tags = []

    for selected_tag in ordered_tags[:max_attempts]:
        if selected_tag in attempted_tags:
            break
        attempted_tags.append(selected_tag)

        search_result = await spider.search_illustrations(selected_tag)
        if not search_result:
            continue

        popular_images = search_result.get("popular", {}).get("recent", [])
        if not popular_images:
            continue

        offset = seed % len(popular_images)
        ordered_images = popular_images[offset:] + popular_images[:offset]
        for selected_image in ordered_images:
            illust_id = selected_image.get("id")
            if not illust_id:
                continue

            illust_details = await spider.get_illust_details(illust_id)
            quality_score = spider.calculate_quality_score(illust_details) if illust_details else None
            bookmark_count = int(illust_details.get("bookmarkCount", 0) or 0) if illust_details else 0
            if bookmark_count <= min_bookmarks:
                continue

            selected_image["bookmarkCount"] = bookmark_count
            return {
                "user_qq": user_id,
                "tag": selected_tag,
                "image": selected_image,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "available_tags_count": len(available_tags),
                "available_images_count": len(popular_images),
                "attempted_tags": attempted_tags,
                "quality_score": quality_score,
            }

    return None
