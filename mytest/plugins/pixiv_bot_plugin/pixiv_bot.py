from nonebot.plugin import PluginMetadata

from .handlers.admin import login_matcher, login_status_matcher, quality_matcher
from .handlers.author import (
    author_info_matcher,
    author_matcher,
    follow_list_matcher,
    follow_matcher,
    unfollow_matcher,
)
from .handlers.core import (
    daily_matcher,
    daily_shortcut_matcher,
    hot_matcher,
    latest_matcher,
    popular_matcher,
    search_matcher,
)
from .handlers.help import help_matcher
from .handlers.info import image_info_matcher
from .handlers.preference import alias_matcher, preference_matcher
from .handlers.stats import popular_tags_matcher, stats_matcher


__plugin_meta__ = PluginMetadata(
    name="pixiv_bot",
    description="Pixiv智能图片搜索机器人",
    usage="基于用户故事地图设计的完整Pixiv图片搜索功能",
)


__all__ = [
    "alias_matcher",
    "author_info_matcher",
    "author_matcher",
    "daily_matcher",
    "daily_shortcut_matcher",
    "follow_list_matcher",
    "follow_matcher",
    "help_matcher",
    "hot_matcher",
    "image_info_matcher",
    "latest_matcher",
    "login_matcher",
    "login_status_matcher",
    "popular_matcher",
    "popular_tags_matcher",
    "preference_matcher",
    "quality_matcher",
    "search_matcher",
    "stats_matcher",
    "unfollow_matcher",
]
