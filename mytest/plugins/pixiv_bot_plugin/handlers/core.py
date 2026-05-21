from nonebot import on_regex
from nonebot.adapters import Event
from nonebot.params import RegexStr
from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import (
    LATEST_MIN_BOOKMARKS,
    extract_plain_text,
    pixiv_command,
)
from ..parser import (
    parse_beautiful_text,
    parse_hot_text,
    parse_latest_text,
    parse_search_text,
)
from ..pixiv import get_pixiv_spider
from ..renderers import send_images_with_info
from ..services.core import (
    beautiful_image,
    daily_recommend as daily_recommend_service,
    hot_images,
    latest_images,
    search_images,
)


daily_recommend = Alconna(pixiv_command("每日一图"))

search_matcher = on_regex(r"^(?:/?p站\s+)?搜图(?:\s+.*)?$", priority=1, block=True)
latest_matcher = on_regex(r"^(?:/?p站\s+)?最新(?:\s+.*)?$", priority=1, block=True)
popular_matcher = on_regex(r"^(?:/?p站\s+)?美图(?:\s+.*)?$", priority=1, block=True)
hot_matcher = on_regex(r"^(?:/?p站\s+)?热门(?:\s+.*)?$", priority=1, block=True)
daily_matcher = on_alconna(daily_recommend, use_cmd_start=True, block=True)
daily_shortcut_matcher = on_regex(r"^(?:/?p站\s+)?每日一图$", priority=1, block=True)


@search_matcher.handle()
async def search_images_handle(event: Event, regex_str: str = RegexStr()):
    parsed = parse_search_text(regex_str or extract_plain_text(event))

    if not parsed:
        await UniMessage.text("请指定搜索标签，例如：p站 搜图 风景 收藏500 3 随机").send()
        return

    count = parsed.count
    english_mode = parsed.mode
    if english_mode not in ["random", "recent", "popular"]:
        await UniMessage.text("模式必须是：随机、最新、热门、美图（或：random、recent、popular、beautiful）").send()
        return

    try:
        if parsed.illust_id:
            await UniMessage.text(f"正在获取作品「{parsed.illust_id}」，请稍候...").send()
        else:
            display_tags = parsed.display_tags
            if parsed.min_bookmarks is not None:
                display_tags = f"{display_tags}（收藏≥{parsed.min_bookmarks}）"
            await UniMessage.text(f"正在搜索「{display_tags}」的{count}张图片（{english_mode}模式），请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await search_images(spider, parsed)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"搜索图片时发生错误：{str(e)}").send()


@latest_matcher.handle()
async def latest_images_handle(event: Event, regex_str: str = RegexStr()):
    parsed = parse_latest_text(regex_str or extract_plain_text(event))

    if not parsed:
        await UniMessage.text("请指定标签，例如：p站 最新 风景 5 24").send()
        return

    tags = parsed.tags
    count = parsed.count
    hours = parsed.hours

    try:
        await UniMessage.text(f"正在获取「{tags}」最近{hours}小时收藏>{LATEST_MIN_BOOKMARKS}的{count}张最新图片，请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await latest_images(spider, parsed)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"获取最新图片时发生错误：{str(e)}").send()


@popular_matcher.handle()
async def popular_images_handle(event: Event, regex_str: str = RegexStr()):
    tags = parse_beautiful_text(regex_str or extract_plain_text(event))

    if not tags:
        await UniMessage.text("请指定标签，例如：p站 美图 萌妹").send()
        return

    try:
        await UniMessage.text(f"正在获取「{tags}」的高收藏精选美图，请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await beautiful_image(spider, tags)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"获取美图时发生错误：{str(e)}").send()


@hot_matcher.handle()
async def hot_images_handle(event: Event, regex_str: str = RegexStr()):
    parsed = parse_hot_text(regex_str or extract_plain_text(event))

    if not parsed:
        await UniMessage.text("请指定标签，例如：p站 热门 萌妹 3").send()
        return

    tags, count = parsed

    try:
        await UniMessage.text(f"正在获取「{tags}」的{count}张热门图片，请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await hot_images(spider, tags, count)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"获取热门图片时发生错误：{str(e)}").send()


async def _handle_daily_recommend(event: Event):
    try:
        await UniMessage.text("正在获取今日推荐图片，请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await daily_recommend_service(spider, event.get_user_id())
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"获取今日推荐时发生错误：{str(e)}").send()


@daily_matcher.handle()
async def daily_recommend_handle(event: Event, result: Arparma):
    await _handle_daily_recommend(event)


@daily_shortcut_matcher.handle()
async def daily_shortcut_handle(event: Event):
    await _handle_daily_recommend(event)


__all__ = [
    "daily_matcher",
    "daily_shortcut_matcher",
    "hot_matcher",
    "latest_matcher",
    "popular_matcher",
    "search_matcher",
]
