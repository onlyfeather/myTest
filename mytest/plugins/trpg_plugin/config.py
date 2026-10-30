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
            SettingField(
                "ask_after",
                "多久问一次「还有人要行动吗」（分钟）",
                type="int",
                default=30,
                minimum=1,
                maximum=24 * 60,
                group="等待行动",
                help="一轮里第一个人行动之后，过了这么久还有人没动，凛音问一次",
            ),
            SettingField(
                "standby_after",
                "问完之后多久按原地待命（小时）",
                type="int",
                default=12,
                minimum=1,
                maximum=72,
                group="等待行动",
                help="问完这么久还没回应的人，这一轮按原地待命，直接结算",
            ),
            SettingField(
                "settle_delay",
                "人齐之后再等多久（秒）",
                type="int",
                default=60,
                minimum=0,
                maximum=600,
                group="等待行动",
                help="所有人都说了要做什么之后，再等这么久没人补充，才开始结算。方便一个人分几条说完",
            ),
        ],
    )
)


def trpg_groups() -> set[str]:
    return {g for g in (str(x).strip() for x in trpg_settings.groups) if g.isdigit()}


def timing():
    from .rounds import Timing

    return Timing(
        ask_after=int(trpg_settings.ask_after) * 60,
        standby_after=int(trpg_settings.standby_after) * 3600,
        settle_delay=int(trpg_settings.settle_delay),
    )


def _resolve_data_dir() -> Path:
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "trpg_plugin"
        logger.warning(f"[跑团] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
