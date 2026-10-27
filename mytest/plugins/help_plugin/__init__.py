from nonebot import on_regex
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata

from .._shared.events import is_superuser
from .._shared.help import HelpTopic, help_registry

__plugin_meta__ = PluginMetadata(
    name="help",
    description="统一帮助",
    usage="/帮助、/帮助 主题",
    type="application",
    supported_adapters={"~onebot.v11"},
)

help_registry.register(
    HelpTopic(
        key="system",
        title="系统",
        aliases=("状态",),
        summary=("/帮助 主题 - 查看某个功能的详细说明",),
        detail="/帮助 - 查看全部功能\n/帮助 主题 - 查看详细说明，主题可以是功能名或指令，例如 /帮助 画图、/帮助 .rd",
        admin=(
            "/status 或 /状态 - 查看服务器 CPU、内存、磁盘",
            "插件配置在网页 /webui 里修改",
        ),
        order=999,
    )
)

help_matcher = on_regex(r"^/(?:帮助|help|菜单)(?:\s+(.+))?\s*$", priority=1, block=True)


@help_matcher.handle()
async def handle_help(bot: Bot, event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    admin = await is_superuser(bot, event)
    query = (groups[0] or "").strip()
    if not query:
        await matcher.finish(help_registry.overview(is_admin=admin))
    topic = help_registry.find(query)
    if topic is None:
        await matcher.finish(help_registry.not_found(query))
    await matcher.finish(help_registry.detail(topic, is_admin=admin))
