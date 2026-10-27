from .._shared.help import HelpTopic, help_registry
from .handlers.help import HELP_TEXT

help_registry.register(
    HelpTopic(
        key="pixiv",
        title="Pixiv",
        aliases=("p站", "搜图", "pixiv图", "每日一图"),
        summary=(
            "搜图 标签 [数量] [模式] - Pixiv 搜图",
            "每日一图 - 个性化推荐",
        ),
        detail=HELP_TEXT,
        order=40,
    )
)
