import random
from typing import Any, Dict, Iterable, List, Optional

from ..quality import score as quality_score


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
    min_quality_score: float = 50.0,
    max_attempts: int = 10,
    source_prefix: str = "random",
    rng: Optional[random.Random] = None,
    **_: Any,
) -> List[Dict[str, Any]]:
    """作者作品的「随机」模式：随机抽 max_attempts 个查详情，按质量分加权挑 count 个。

    质量分达到阈值的优先；都达不到时按分数从高到低补上，不会因为阈值发不出图。
    """
    rng = rng or random.Random()
    pool = [item for item in candidates if item and item.get("id")]
    rng.shuffle(pool)
    scored = []
    for candidate in pool[: max(max_attempts, count)]:
        details = await spider.get_illust_details(candidate["id"])
        if not details:
            continue
        value = quality_score(details).total
        scored.append((value, candidate, int(details.get("bookmarkCount") or 0)))

    good = [item for item in scored if item[0] >= min_quality_score]
    rest = sorted((item for item in scored if item[0] < min_quality_score), key=lambda item: item[0], reverse=True)
    picked = []
    while good and len(picked) < count:
        index = rng.choices(range(len(good)), weights=[max(v, 1.0) ** 2 for v, _, _ in good])[0]
        picked.append(good.pop(index))
    picked += rest[: count - len(picked)]
    return [
        await build_image_info(spider, candidate, f"{source_prefix}_selection",
                               quality_score=round(value, 1), bookmark_count=bookmarks)
        for value, candidate, bookmarks in picked
    ]
