from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import pixiv_command
from ..pixiv import get_pixiv_spider
from ..services.stats import build_popular_tags_message, build_stats_message


stats_cmd = Alconna(pixiv_command("统计"))
popular_tags = Alconna(pixiv_command("热门标签"))

stats_matcher = on_alconna(stats_cmd, use_cmd_start=True, block=True)
popular_tags_matcher = on_alconna(popular_tags, use_cmd_start=True, block=True)


@stats_matcher.handle()
async def stats_handle(result: Arparma):
    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(build_stats_message(spider)).send()

    except Exception as e:
        await UniMessage.text(f"获取统计信息时发生错误：{str(e)}").send()


@popular_tags_matcher.handle()
async def popular_tags_handle(result: Arparma):
    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(build_popular_tags_message(spider)).send()

    except Exception as e:
        await UniMessage.text(f"获取热门标签时发生错误：{str(e)}").send()


__all__ = [
    "popular_tags_matcher",
    "stats_matcher",
]
