from pathlib import Path

from nonebot import logger

from .._shared.settings import SettingField, SettingSection, register

trpg_settings = register(
    SettingSection(
        id="trpg",
        title="跑团",
        description="凛音当 KP（开发中）。只在下面填的群里生效，这些群里 MaiBot 要用「跑团群静默」插件关掉。",
        order=60,
        fields=[
            SettingField(
                "groups",
                "跑团群",
                type="lines",
                default=[],
                help="每行一个群号。只填专门用来跑团的群：团进行中，群里所有不带括号的发言都会当成角色行动",
            ),
        ],
    )
)


def trpg_groups() -> set[str]:
    return {g for g in (str(x).strip() for x in trpg_settings.groups) if g.isdigit()}


def _resolve_data_dir() -> Path:
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "trpg_plugin"
        logger.warning(f"[跑团] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
