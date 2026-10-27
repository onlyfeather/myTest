import os
from pathlib import Path

from nonebot import get_driver, logger

from .._shared.settings import SettingField, SettingSection, llm_settings, register

# 大模型的地址、密钥和模型在共用的「大模型」分区里，这里只放骰娘自己的开关。
dice_settings = register(
    SettingSection(
        id="dice_girl",
        title="骰娘",
        description="投骰检定，并由凛音用 AI 点评结果。",
        order=20,
        fields=[
            SettingField(
                "ai_enabled",
                "AI 点评",
                type="bool",
                default=True,
                help="关闭后只显示投骰结果和一句固定点评",
                env=("dice_girl_ai_enabled",),
            ),
            SettingField(
                "voice_reply",
                "同时发语音",
                type="bool",
                default=False,
                help="点评文字之外再用 Fish Audio 合成一条语音（需要先配置「语音合成」）",
            ),
        ],
    )
)


class _Config:
    """兼容旧代码的 config.ai_xxx 写法，每次读取都是最新配置。"""

    @property
    def ai_api_key(self) -> str:
        return str(llm_settings.api_key)

    @property
    def ai_base_url(self) -> str:
        return str(llm_settings.base_url)

    @property
    def ai_model(self) -> str:
        return str(llm_settings.model)

    @property
    def ai_timeout(self) -> float:
        return float(llm_settings.timeout)

    @property
    def ai_max_tokens(self) -> int:
        return int(llm_settings.max_tokens)

    @property
    def ai_json_mode(self) -> bool:
        return bool(llm_settings.json_mode)

    @property
    def ai_proxy(self) -> str:
        return str(llm_settings.proxy)

    @property
    def ai_enabled(self) -> bool:
        return bool(dice_settings.ai_enabled)

    data_dir = (
        str(getattr(get_driver().config, "dice_girl_data_dir", "") or "")
        or os.environ.get("DICE_GIRL_DATA_DIR")
        or None
    )


config = _Config()


def _resolve_data_dir() -> Path:
    if config.data_dir:
        return Path(config.data_dir)
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "dice_girl_plugin"
        logger.warning(f"[DiceGirl] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"[DiceGirl] 数据目录: {DATA_DIR.resolve()}")


def get_data_path(filename: str) -> Path:
    return DATA_DIR / filename
