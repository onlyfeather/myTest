"""把 NoneBot 的功能开放给 MaiBot：凛音聊天时可以调用画图、自拍、语音、投骰、Pixiv 搜图。

接口：POST /tools/{tool}（仅限本机访问，需要 TOOLS_API_TOKEN）。
结果（图片、语音）由 NoneBot 直接发到群或私聊里，接口只返回给凛音看的文字说明。

.env / docker-compose 环境变量：
    TOOLS_API_TOKEN=一个随机长字符串   # MaiBot 插件里填同一个值；不设置就不开放
"""

import os

from nonebot import get_app, get_driver, logger, require
from nonebot.drivers import ASGIMixin
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")

# 各功能插件在工具被调用时才导入（见 tools.py），这里不用 require：
# 它们和本插件在同一个插件目录里，NoneBot 启动时会一起加载。

from .._shared.help import HelpTopic, help_registry
from .._shared.settings import settings_store
from .selfie import list_persona_status, selfie_settings
from .server import create_router
from .tools import TOOLS

__plugin_meta__ = PluginMetadata(
    name="tools_api",
    description="给 MaiBot 调用的工具接口",
    usage="由 MaiBot 的凛音工具插件调用，不提供聊天指令",
    type="application",
    supported_adapters={"~onebot.v11"},
)

MIN_TOKEN_LENGTH = 24


def _token() -> str:
    value = getattr(get_driver().config, "tools_api_token", None) or os.environ.get("TOOLS_API_TOKEN", "")
    value = str(value or "").strip()
    return value if len(value) >= MIN_TOKEN_LENGTH else ""


settings_store.set_tester("selfie", list_persona_status)

help_registry.register(
    HelpTopic(
        key="chat_tools",
        title="和凛音说",
        aliases=("凛音", "聊天", "自拍", "工具"),
        summary=(
            "不用记指令，直接跟凛音说就行：画图、自拍、语音、投骰、Pixiv 搜图和每日一图她都能直接做",
            "例：「帮我画…」「发张自拍」「用语音说…」「帮我投个侦查」「找几张…的图」",
        ),
        detail=(
            "和凛音聊天时可以直接提要求，她会自己调用对应的功能：\n"
            "帮我画一张海边的猫娘 - 画图（回复一张图再说「照着这张画」也行）\n"
            "发张自拍看看 - 凛音的自拍，会根据当时的情景来拍\n"
            "用语音跟我说晚安 - 语音\n"
            "帮我投个侦查，60 - 投骰，结果是真随机的\n"
            "找几张初音的图 / 今天的每日一图 - Pixiv\n\n"
            "她嘴上可能会嫌麻烦，但会去做。\n"
            "想要固定的效果（尺寸、画师串、参考图）还是用 /nai、/gpt画图 这些指令更准确。"
        ),
        admin=("让凛音唱歌、说大段的话（讲故事、念长文）只有管理员能用",),
        order=5,
    )
)

driver = get_driver()
if isinstance(driver, ASGIMixin):
    get_app().include_router(create_router(TOOLS, _token))
    if _token():
        logger.info("[工具接口] 已开启：{}", "、".join(TOOLS))
    else:
        logger.warning("[工具接口] 没有设置 TOOLS_API_TOKEN（至少 {} 位），凛音无法调用工具", MIN_TOKEN_LENGTH)

__all__ = ["selfie_settings"]
