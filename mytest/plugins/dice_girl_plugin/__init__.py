from nonebot import require

require("nonebot_plugin_localstore")

from nonebot.plugin import PluginMetadata

from . import handlers as handlers

__plugin_meta__ = PluginMetadata(
    name="dice_girl",
    description="骰娘：投骰检定，并由角色用 AI 点评结果",
    usage="发送「/dice 帮助」查看全部指令",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
