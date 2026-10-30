"""搜图、最新、美图、热门、每日一图：把指令参数转成 SearchRequest，再把结果整理成要发的图片。"""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import List

from ..common import BEAUTIFUL_THRESHOLDS, LATEST_MIN_BOOKMARKS, RANDOM_MIN_BOOKMARKS, pixiv_failure_message
from ..pixiv.errors import PixivError, describe
from ..search import SearchRequest
from ..seen import seen_tracker
from .image_selection import build_image_info


@dataclass
class ImageResult:
    ok: bool
    message: str
    images: List[dict] | None = None
    title: str = ""
    display_tags: str = ""
    effective_min_bookmarks: int = 0


async def _to_images(spider, found: List[dict], source: str) -> List[dict]:
    images = []
    for item in found:
        image = await build_image_info(spider, item, source, bookmark_count=item.get("bookmarkCount"))
        image["quality_score"] = item.get("quality_score")
        images.append(image)
    return images


async def _run(spider, req: SearchRequest, scope: str, what: str, source: str) -> tuple[List[dict], str]:
    """跑一次搜索。返回 (图片, 失败说明)；成功时失败说明为空，并记下这个会话发过的作品。"""
    req.exclude = req.exclude | seen_tracker.seen(scope)
    try:
        outcome = await spider.search(req)
    except PixivError as e:
        return [], f"{what}失败：{e}"
    if not outcome.images:
        return [], f"{what}没有找到合适的图。\n原因：{describe(outcome.reason)}"
    images = await _to_images(spider, outcome.images, source)
    seen_tracker.mark(scope, [str(image["id"]) for image in images])
    return images, ""


async def search_images(spider, parsed, scope: str = "") -> ImageResult:
    """scope 是会话（群号 / 私聊 QQ 号），同一个会话三天内发过的作品不再发。"""
    if parsed.illust_id:
        return await _search_by_id(spider, parsed)

    min_bookmarks = parsed.min_bookmarks if parsed.min_bookmarks is not None else (
        RANDOM_MIN_BOOKMARKS if parsed.mode == "random" else 0)
    display_tags = parsed.display_tags + (f"（收藏≥{min_bookmarks}）" if min_bookmarks else "")
    req = SearchRequest(keyword=parsed.display_tags, count=parsed.count, mode=parsed.mode,
                        min_bookmarks=min_bookmarks, min_quality=float(spider.min_quality_score))
    images, error = await _run(spider, req, scope, f"搜索「{display_tags}」", f"search_{parsed.mode}")
    if error:
        return ImageResult(False, error, display_tags=display_tags, effective_min_bookmarks=min_bookmarks)
    title = f"搜索结果「{display_tags}」"
    if len(images) < parsed.count:
        title += f"（只找到 {len(images)} 张合适的）"
    return ImageResult(True, "", images=images, title=title, display_tags=display_tags,
                       effective_min_bookmarks=min_bookmarks)


async def _search_by_id(spider, parsed) -> ImageResult:
    image = await spider.get_image_info(parsed.illust_id)
    if not image:
        return ImageResult(
            False,
            pixiv_failure_message(spider, "获取作品", parsed.illust_id, "请确认作品ID正确；也可以用 p站 登录状态 检查 Cookie。"),
        )
    return ImageResult(True, "", images=[image], title=f"作品「{parsed.illust_id}」", display_tags=parsed.display_tags)


async def latest_images(spider, parsed, scope: str = "") -> ImageResult:
    """最近 hours 小时内发布、收藏过 LATEST_MIN_BOOKMARKS 的作品，从新到旧。

    新作品还没被打上「users入り」标签，所以不用标签，从第 1 页按时间往后翻，翻到超出时间范围就停。
    """
    what = f"最新图片「{parsed.tags}」（{parsed.hours} 小时内）"
    req = SearchRequest(keyword=parsed.tags, count=parsed.count, mode="recent", min_bookmarks=LATEST_MIN_BOOKMARKS,
                        max_age_hours=parsed.hours, max_rounds=6, detail_batch=max(12, parsed.count * 3))
    images, error = await _run(spider, req, scope, what, "tag_latest")
    if error:
        if "没有找到" in error and parsed.hours < 168:
            error += f"\n可以把时间范围放宽，例如：p站 最新 {parsed.tags} {parsed.count} 168"
        return ImageResult(False, error)
    title = f"最新图片「{parsed.tags}」（{parsed.hours}小时内，收藏>{LATEST_MIN_BOOKMARKS}）"
    if len(images) < parsed.count:
        title += f"（只找到 {len(images)} 张）"
    return ImageResult(True, "", images=images, title=title)


async def beautiful_image(spider, tags: str, scope: str = "") -> ImageResult:
    """高收藏精选 1 张：收藏 >5000 找不到就依次降到 >2500、>1000、>500。"""
    last = None
    for threshold in BEAUTIFUL_THRESHOLDS:
        params = SimpleNamespace(display_tags=tags, count=1, mode="popular", min_bookmarks=threshold, illust_id=None)
        last = await search_images(spider, params, scope)
        if last.ok:
            last.title = f"美图推荐「{tags}」（收藏>{threshold}）"
            return last
        if "失败：" in last.message:  # Cookie、网络这类问题，换阈值也没用
            return last
    return last or ImageResult(False, "没有找到美图")


async def hot_images(spider, tags: str, count: int, scope: str = "") -> ImageResult:
    params = SimpleNamespace(display_tags=tags, count=count, mode="popular", min_bookmarks=0, illust_id=None)
    result = await search_images(spider, params, scope)
    if result.ok:
        result.title = f"热门推荐「{tags}」"
    return result


async def daily_recommend(spider, user_id: str) -> ImageResult:
    """每人每天一张：第一次要时按「日期 + QQ 号」挑一张存起来，当天再要就是同一张（重启也不变）。"""
    if not spider.preferred_tags:
        return ImageResult(False, "获取今日推荐失败：还没有喜好标签，请先用「p站 偏好 喜欢 标签」添加。")
    try:
        picked = await spider.daily.pick(user_id or "anonymous")
    except PixivError as e:
        return ImageResult(False, f"获取今日推荐失败：{e}")
    if picked is None:
        tags = "、".join(sorted(spider.preferred_tags)[:5])
        return ImageResult(False, f"今日推荐没有找到合适的图（试了喜好标签：{tags}）。\n"
                                  "可以添加更常见的日文标签，例如 風景、オリジナル：p站 偏好 喜欢 風景")
    image = dict(picked.image)
    if picked.fresh:
        image = await build_image_info(spider, image, "daily", bookmark_count=image.get("bookmarkCount"))
        image["quality_score"] = picked.image.get("quality_score")
        spider.storage.save_daily(user_id or "anonymous", _today(), str(image["id"]), picked.tag, image)
    elif not image.get("urls"):
        image["urls"] = await spider.get_illust_original_urls(image["id"]) or [image.get("url", "")]
    return ImageResult(True, "", images=[image], title=f"📅 今日推荐「{picked.tag}」")


def _today() -> str:
    from ..daily import today

    return today()
