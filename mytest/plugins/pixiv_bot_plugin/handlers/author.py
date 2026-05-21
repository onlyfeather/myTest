from nonebot import logger
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import Image, UniMessage

from ..common import pixiv_command
from ..parser import convert_mode_to_english
from ..pixiv import get_pixiv_spider
from ..renderers import send_images_with_info
from ..services.author import (
    author_info_progress_message,
    build_author_info_message,
    build_follow_list_message,
    build_latest_images_message,
    follow_author as follow_author_service,
    get_author_images,
    get_author_info,
    unfollow_author as unfollow_author_service,
)


author_images = Alconna(
    pixiv_command("作者"),
    Args["user_id", str]["count", int, 3]["mode", str, "recent"],
)

follow_author = Alconna(
    pixiv_command("关注"),
    Args["user_id", str]["author_name", str, ""],
)

unfollow_author = Alconna(
    pixiv_command("取关"),
    Args["user_id", str],
)

follow_list = Alconna(pixiv_command("关注列表"))

author_info = Alconna(
    pixiv_command("作者信息"),
    Args["user_id", str],
)


author_matcher = on_alconna(author_images, use_cmd_start=True, block=True)
follow_matcher = on_alconna(follow_author, use_cmd_start=True, block=True)
unfollow_matcher = on_alconna(unfollow_author, use_cmd_start=True, block=True)
follow_list_matcher = on_alconna(follow_list, use_cmd_start=True, block=True)
author_info_matcher = on_alconna(author_info, use_cmd_start=True, block=True)


@author_matcher.handle()
async def author_images_handle(result: Arparma):
    user_id = result.query[str]("user_id")
    mode = result.query[str]("mode")
    count = result.query[int]("count")

    if not user_id:
        await UniMessage.text("请指定作者ID或圈名，例如：p站 作者 123456 3 随机").send()
        return

    english_mode = convert_mode_to_english(mode or "")
    preview_count = min(max(count or 1, 1), 5)
    await UniMessage.text(f"正在获取作者「{user_id}」的{preview_count}张作品（{english_mode}模式），请稍候...").send()

    try:
        async with get_pixiv_spider() as spider:
            result = await get_author_images(spider, user_id, mode, count)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return
            await send_images_with_info(result.images or [], result.title)

    except Exception as e:
        await UniMessage.text(f"获取作者作品时发生错误：{str(e)}").send()


@follow_matcher.handle()
async def follow_author_handle(result: Arparma):
    user_id = result.query[str]("user_id")
    author_name = result.query[str]("author_name")

    if not user_id:
        await UniMessage.text("请指定作者ID，例如：p站 关注 123456 画师名").send()
        return

    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(follow_author_service(spider, user_id, author_name)).send()

    except Exception as e:
        await UniMessage.text(f"关注作者时发生错误：{str(e)}").send()


@unfollow_matcher.handle()
async def unfollow_author_handle(result: Arparma):
    user_id = result.query[str]("user_id")

    if not user_id:
        await UniMessage.text("请指定作者ID，例如：p站 取关 123456").send()
        return

    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(unfollow_author_service(spider, user_id)).send()

    except Exception as e:
        await UniMessage.text(f"取关作者时发生错误：{str(e)}").send()


@follow_list_matcher.handle()
async def follow_list_handle(result: Arparma):
    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(build_follow_list_message(spider)).send()

    except Exception as e:
        await UniMessage.text(f"获取关注列表时发生错误：{str(e)}").send()


@author_info_matcher.handle()
async def author_info_handle(result: Arparma):
    user_id = result.query[str]("user_id")

    if not user_id:
        await UniMessage.text("请指定作者ID或圈名，例如：p站 作者信息 12345678").send()
        return

    try:
        async with get_pixiv_spider() as spider:
            original_input = str(user_id).strip()
            resolved_user_id = spider.search_author_by_alias(original_input) or original_input
            await UniMessage.text(author_info_progress_message(original_input, resolved_user_id)).send()
            result = await get_author_info(spider, user_id)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return

            author_info = result.author_info or {}
            image = author_info.get("image", "")
            if image:
                try:
                    avatar_message = UniMessage.text(f"👤 作者信息（ID: {result.resolved_user_id}）\n")
                    avatar_message = avatar_message + Image(url=image)
                    await avatar_message.send()
                except Exception as e:
                    logger.error(f"发送头像失败: {e}")

            await UniMessage.text(build_author_info_message(result)).send()
            if author_info.get("latestImages"):
                await UniMessage.text(build_latest_images_message(author_info)).send()

    except Exception as e:
        await UniMessage.text(f"获取作者信息时发生错误：{str(e)}").send()


__all__ = [
    "author_info_matcher",
    "author_matcher",
    "follow_list_matcher",
    "follow_matcher",
    "unfollow_matcher",
]
