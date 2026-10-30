from pathlib import Path

from nonebot import logger

from .._shared.settings import SettingField, SettingSection, register

FALLBACK_SLOTS = 3
ROLES = {"arbiter": "裁决", "narrator": "叙述", "reviewer": "审查"}


def _model_fields() -> list[SettingField]:
    fields = [
        SettingField(
            f"{role}_model",
            f"{label}用的模型",
            default="",
            group="模型",
            help="留空用「大模型」分区里的模型。接口地址和密钥都用「大模型」分区的。"
                 + ("裁决需要稳定输出 JSON，建议用能力强一些的模型" if role == "arbiter" else ""),
        )
        for role, label in ROLES.items()
    ]
    fields.append(SettingField(
        "output_mode",
        "结构化输出方式",
        type="select",
        default="schema",
        options=(("schema", "JSON Schema（最稳，接口要支持）"), ("json", "JSON 模式"), ("plain", "只靠提示词")),
        group="模型",
        help="接口不支持时会自动退一档。不管哪种方式，代码都会再校验一遍",
    ))
    for i in range(1, FALLBACK_SLOTS + 1):
        group = f"兜底模型 {i}"
        fields += [
            SettingField(f"fallback{i}_model", "模型", default="", group=group,
                         help="主模型连续失败 3 次后按顺序换兜底模型。留空表示不用这一项"),
            SettingField(f"fallback{i}_base_url", "接口地址", default="", group=group,
                         placeholder="https://api.deepseek.com/v1",
                         help="留空表示和「大模型」分区用同一个接口和密钥；填了别的服务商就要在下面填它的密钥"),
            SettingField(f"fallback{i}_api_key", "API Key", type="secret", default="", group=group),
        ]
    return fields


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
            *_model_fields(),
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


def endpoints(role: str):
    """某个环节（arbiter / narrator / reviewer）按顺序可用的模型。"""
    from .._shared.settings import llm_settings
    from .ai import build_endpoints

    main = {k: getattr(llm_settings, k) for k in ("base_url", "api_key", "model", "proxy", "timeout")}
    fallbacks = [
        {k: getattr(trpg_settings, f"fallback{i}_{k}") for k in ("base_url", "api_key", "model")}
        for i in range(1, FALLBACK_SLOTS + 1)
    ]
    return build_endpoints(main, str(getattr(trpg_settings, f"{role}_model")), fallbacks)


def output_mode() -> str:
    return str(trpg_settings.output_mode)


def _resolve_data_dir() -> Path:
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "trpg_plugin"
        logger.warning(f"[跑团] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
