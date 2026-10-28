"""聊天预设：NSFW 模式和强攻击力模式。

预设内容从酒馆导入，保存在 MYTEST_PRESETS_FILE（默认和配置文件放在同一目录的 presets.json）；
开关和白名单是「聊天预设」配置分区。MaiBot 的凛音工具插件在每次回复前通过工具接口取一次提示词。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..settings import SETTINGS_FILE, SettingField, SettingSection, register
from .core import (
    MODES,
    Entry,
    Mode,
    PresetError,
    PresetStore,
    build_prompt,
    parse_st_preset,
)

__all__ = [
    "MODES",
    "Entry",
    "Mode",
    "PresetError",
    "PresetStore",
    "build_prompt",
    "parse_st_preset",
    "preset_settings",
    "preset_store",
]

PRESETS_FILE = Path(os.environ.get("MYTEST_PRESETS_FILE") or SETTINGS_FILE.with_name("presets.json"))
preset_store = PresetStore(PRESETS_FILE)

ID_HELP = "每行一个 QQ 号"
preset_settings = register(
    SettingSection(
        id="presets",
        title="聊天预设",
        description=(
            "凛音在 MaiBot 里聊天时额外套用的酒馆预设。预设内容在左侧「预设：…」页面导入和编辑，"
            "这里控制开关和生效范围。需要 MaiBot 凛音工具插件 1.4.0 以上，并打开它的「聊天预设」开关。"
        ),
        order=15,
        fields=[
            SettingField("char_name", "角色名", default="白鸟凛音", help="预设里的 {{char}} 会换成这个名字"),
            SettingField(
                "nsfw_enabled", "NSFW 模式", type="bool", default=False, group="NSFW 模式",
                help="只在下面列出的私聊和群里生效。打开前请确认这些人和群里的成员都已成年",
            ),
            SettingField("nsfw_users", "NSFW 允许的私聊", type="lines", default=[], group="NSFW 模式", help=ID_HELP),
            SettingField(
                "nsfw_groups", "NSFW 允许的群", type="lines", default=[], group="NSFW 模式",
                help="每行一个群号。群里所有人都会看到回复",
            ),
            SettingField(
                "aggressive_enabled", "强攻击力模式", type="bool", default=False, group="强攻击力模式",
                help="私聊总是生效，群里只在下面列出的群生效",
            ),
            SettingField(
                "aggressive_groups", "强攻击力允许的群", type="lines", default=[], group="强攻击力模式",
                help="每行一个群号",
            ),
        ],
    )
)


def prompt_for(user_id: str, group_id: str | None, user_name: str) -> tuple[str, list[str]]:
    return build_prompt(
        preset_store, preset_settings, user_id, group_id,
        char=str(preset_settings.get("char_name") or "白鸟凛音"), user=user_name or "对方",
    )
