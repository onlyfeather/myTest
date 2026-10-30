"""凛音当 KP：在专门的跑团群里，由凛音主持 CoC 7 版的异步跑团。设计见 docs/trpg-kp-design.md。

还在开发中：现在能开团、加入、开始、暂停、继续、结束、离开，还不会处理角色行动。
"""

from nonebot import require

require("nonebot_plugin_localstore")

from nonebot.plugin import PluginMetadata

from . import handlers as handlers
from . import help as help  # noqa: A004  登记到统一帮助

__plugin_meta__ = PluginMetadata(
    name="trpg",
    description="凛音当 KP：CoC 7 版异步跑团（开发中）",
    usage="发送「/帮助 KP」查看指令",
    type="application",
    supported_adapters={"~onebot.v11"},
)
