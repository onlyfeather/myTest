from .admin import login_matcher, login_status_matcher, quality_matcher
from .author import (
    author_info_matcher,
    author_matcher,
    follow_list_matcher,
    follow_matcher,
    unfollow_matcher,
)
from .core import (
    daily_matcher,
    daily_shortcut_matcher,
    hot_matcher,
    latest_matcher,
    popular_matcher,
    search_matcher,
)
from .help import help_matcher
from .info import image_info_matcher
from .preference import alias_matcher, preference_matcher
from .stats import popular_tags_matcher, stats_matcher

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
