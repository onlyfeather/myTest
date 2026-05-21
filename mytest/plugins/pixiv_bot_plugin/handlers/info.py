from nonebot import logger
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import Image, UniMessage

from ..common import pixiv_command
from ..pixiv import get_pixiv_spider
from ..renderers import extract_image_urls
from ..services.info import build_image_info_message, get_image_info


image_info = Alconna(
    pixiv_command("图片信息"),
    Args["illust_id", str],
)

image_info_matcher = on_alconna(image_info, use_cmd_start=True, block=True)


@image_info_matcher.handle()
async def image_info_handle(result: Arparma):
    illust_id = result.query[str]("illust_id")

    if not illust_id:
        await UniMessage.text("请指定图片ID，例如：p站 图片信息 123456789").send()
        return

    try:
        await UniMessage.text(f"正在获取图片信息（ID: {illust_id}），请稍候...").send()

        async with get_pixiv_spider() as spider:
            result = await get_image_info(spider, illust_id)
            if not result.ok:
                await UniMessage.text(result.message).send()
                return

            image_info = result.image_info or {}
            message_text = build_image_info_message(image_info, illust_id)
            urls = extract_image_urls(image_info)
            if urls:
                try:
                    message = UniMessage.text(f"📷 图片信息（ID: {illust_id}）\n")
                    for url in urls:
                        message = message + Image(url=url)
                    await message.send()
                    await UniMessage.text(message_text).send()
                except Exception as e:
                    logger.error(f"发送图片失败: {e}")
                    await UniMessage.text(message_text + "\n图片发送失败").send()
            else:
                await UniMessage.text(message_text).send()

    except Exception as e:
        error_type = type(e).__name__
        logger.error(f"图片信息获取失败 - ID: {illust_id}")
        logger.error(f"错误类型: {error_type}")
        logger.error(f"错误信息: {str(e)}")
        logger.error(f"错误详情: {repr(e)}")

        if "slice" in str(e).lower():
            logger.error("检测到slice相关错误，可能原因：")
            logger.error("1. 图片数据结构不完整")
            logger.error("2. URL数组格式异常")
            logger.error("3. 数据类型转换失败")

            try:
                if "image_info" in locals() and image_info is not None:
                    logger.error(f"image_info类型: {type(image_info)}")
                    logger.error(f"image_info内容: {image_info}")
                    urls_field = image_info.get("urls", "MISSING") if isinstance(image_info, dict) else "NOT_DICT"
                    url_field = image_info.get("url", "MISSING") if isinstance(image_info, dict) else "NOT_DICT"
                    logger.error(f"urls字段: {urls_field} (类型: {type(urls_field)})")
                    logger.error(f"url字段: {url_field} (类型: {type(url_field)})")
                else:
                    logger.error("image_info变量不存在或为None")
            except Exception as debug_e:
                logger.error(f"获取调试信息失败: {debug_e}")

        user_msg = f"获取图片信息时发生错误：{error_type} - {str(e)}"
        if "slice" in str(e).lower():
            user_msg += "\n🔍 数据格式异常，请稍后重试"

        await UniMessage.text(user_msg).send()


__all__ = ["image_info_matcher"]
