import random
from typing import Any, Dict, Iterable, List, Optional

from nonebot import logger


def build_search_statistics(search_result: Dict[str, Any]) -> Dict[str, int]:
    popular = search_result.get("popular", {})
    return {
        "total_search_results": int(search_result.get("total", 0) or 0),
        "popular_recent_count": len(popular.get("recent", []) or []),
        "popular_permanent_count": len(popular.get("permanent", []) or []),
        "regular_illust_count": len(search_result.get("illusts", []) or []),
    }


def get_search_candidates(search_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    popular = search_result.get("popular", {})
    candidates: List[Dict[str, Any]] = []
    candidates.extend(popular.get("recent", []) or [])
    candidates.extend(popular.get("permanent", []) or [])
    candidates.extend(search_result.get("illusts", []) or [])
    return candidates


async def build_image_info(
    spider,
    candidate: Dict[str, Any],
    source: str,
    *,
    quality_score: Optional[Dict[str, Any]] = None,
    bookmark_count: Optional[int] = None,
) -> Dict[str, Any]:
    image_id = candidate.get("id", "")
    original_urls = await spider.get_illust_original_urls(image_id)
    primary_url = original_urls[0] if original_urls else candidate.get("url", "")

    image_info = {
        "id": image_id,
        "title": candidate.get("title", ""),
        "urls": original_urls if original_urls else [primary_url],
        "tags": candidate.get("tags", []),
        "userId": candidate.get("userId", ""),
        "userName": candidate.get("userName", ""),
        "pageCount": candidate.get("pageCount", 1),
        "width": candidate.get("width", 0),
        "height": candidate.get("height", 0),
        "illustType": candidate.get("illustType", 0),
        "xRestrict": candidate.get("xRestrict", 0),
        "description": candidate.get("description", ""),
        "createDate": candidate.get("createDate", ""),
        "aiType": candidate.get("aiType", 0),
        "profileImageUrl": candidate.get("profileImageUrl", ""),
        "source": source,
    }
    if quality_score is not None:
        image_info["quality_score"] = quality_score
    if bookmark_count is not None:
        image_info["bookmarkCount"] = bookmark_count
    elif candidate.get("bookmarkCount") is not None:
        image_info["bookmarkCount"] = candidate.get("bookmarkCount")
    return image_info


async def build_images_from_candidates(
    spider,
    candidates: Iterable[Dict[str, Any]],
    count: int,
    source: str,
    *,
    shuffle: bool = False,
) -> List[Dict[str, Any]]:
    candidate_list = [item for item in candidates if item and item.get("id")]
    if shuffle:
        selected = random.sample(candidate_list, min(count, len(candidate_list)))
    else:
        selected = candidate_list[:count]
    return [await build_image_info(spider, item, source) for item in selected]


async def select_quality_random_images(
    spider,
    candidates: Iterable[Dict[str, Any]],
    count: int,
    *,
    min_quality_score: float = 60.0,
    max_attempts: int = 10,
    min_bookmarks: int = 0,
    allow_fallback: bool = True,
    source_prefix: str = "random",
) -> List[Dict[str, Any]]:
    candidate_list = [item for item in candidates if item and item.get("id")]
    if not candidate_list:
        logger.debug("选图候选池为空")
        return []

    images: List[Dict[str, Any]] = []
    failed_candidates: List[Dict[str, Any]] = []
    used_image_ids = set()
    attempts = 0

    while len(images) < count and attempts < max_attempts:
        attempts += 1
        candidate = random.choice(candidate_list)
        image_id = candidate.get("id")
        if not image_id or image_id in used_image_ids:
            continue

        used_image_ids.add(image_id)
        quality_score = None
        illust_details = None
        try:
            illust_details = await spider.get_illust_details(image_id)
            if illust_details:
                quality_score = spider.calculate_quality_score(illust_details)
        except Exception as e:
            logger.debug(f"质量评分计算异常: {e}")

        bookmark_count = int(illust_details.get("bookmarkCount", 0) or 0) if illust_details else 0
        if (
            quality_score
            and quality_score.get("total_score", 0) >= min_quality_score
            and bookmark_count > min_bookmarks
        ):
            images.append(
                await build_image_info(
                    spider,
                    candidate,
                    f"{source_prefix}_selection",
                    quality_score=quality_score,
                    bookmark_count=bookmark_count,
                )
            )
            continue

        failed_candidates.append(
            {
                "candidate": candidate,
                "quality_score": quality_score,
                "bookmark_count": bookmark_count,
                "reason": "low_bookmarks"
                if bookmark_count <= min_bookmarks
                else ("low_quality" if quality_score else "no_score"),
            }
        )

    if allow_fallback and min_bookmarks <= 0 and len(images) < count and failed_candidates:
        failed_candidates.sort(
            key=lambda item: (
                0 if item["quality_score"] else 1,
                -(item["quality_score"].get("total_score", 0) if item["quality_score"] else 0),
            )
        )
        needed = count - len(images)
        for item in failed_candidates[:needed]:
            images.append(
                await build_image_info(
                    spider,
                    item["candidate"],
                    f"{source_prefix}_fallback",
                    quality_score=item["quality_score"],
                    bookmark_count=item["bookmark_count"],
                )
            )

    if allow_fallback and min_bookmarks <= 0 and len(images) < count:
        used = {image.get("id") for image in images}
        remaining = [item for item in candidate_list if item.get("id") not in used]
        needed = count - len(images)
        for candidate in random.sample(remaining, min(needed, len(remaining))):
            images.append(await build_image_info(spider, candidate, f"{source_prefix}_final"))

    logger.debug(f"随机选图完成: 尝试 {attempts} 次，返回 {len(images)} 张")
    return images
