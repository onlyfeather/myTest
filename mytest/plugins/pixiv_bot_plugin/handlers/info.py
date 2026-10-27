from nonebot import logger
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import pixiv_command
from ..pixiv import get_pixiv_spider
from ..renderers import extract_image_urls, send_text_with_images
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
            urls = extract_image_urls(image_info)
            if urls:
                await send_text_with_images(f"📷 图片信息（ID: {illust_id}）\n", urls)
            await UniMessage.text(build_image_info_message(image_info, illust_id)).send()

    except Exception as e:
        logger.exception(f"[Pixiv] 图片信息获取失败 - ID: {illust_id}")
        await UniMessage.text(f"获取图片信息时发生错误：{type(e).__name__}，详情见后台日志").send()


__all__ = ["image_info_matcher"]
