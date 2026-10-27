from nonebot import require

require("nonebot_plugin_alconna")
require("nonebot_plugin_localstore")

from nonebot.plugin import PluginMetadata

from . import handlers as handlers
from . import help as help  # noqa: A004  登记到统一帮助

__plugin_meta__ = PluginMetadata(
    name="pixiv_bot",
    description="Pixiv 图片搜索机器人",
    usage="发送「p站 帮助」查看全部指令",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
