"""凛音当 KP：在专门的跑团群里，由凛音主持 CoC 7 版的异步跑团。设计见 docs/trpg-kp-design.md。

还在开发中，目前只有模组格式和校验，没有指令。
"""

from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="trpg",
    description="凛音当 KP：CoC 7 版异步跑团（开发中）",
    usage="开发中，暂无指令",
    type="application",
    supported_adapters={"~onebot.v11"},
)
