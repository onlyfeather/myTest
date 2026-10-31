"""全局配置中心。

各插件在这里注册自己的配置分区，WebUI 负责展示和修改，插件每次使用时读取最新值，改完不用重启。
配置保存在 MYTEST_SETTINGS_FILE（默认 data/settings/settings.json），没有在 WebUI 里改过的项
仍然读取 .env / 环境变量，所以原来的 .env 配置继续有效。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import proxy as _proxy
from .registry import (
    AccountPanel,
    FileSlot,
    ItemManager,
    ManagerAction,
    Picker,
    SectionView,
    SettingField,
    SettingSection,
    SettingsStore,
    coerce,
)

__all__ = [
    "AccountPanel",
    "FileSlot",
    "ItemManager",
    "ManagerAction",
    "Picker",
    "SectionView",
    "SettingField",
    "SettingSection",
    "SettingsStore",
    "coerce",
    "llm_settings",
    "proxy_for",
    "proxy_settings",
    "register",
    "settings_store",
]


def _env_lookup(name: str) -> Any:
    try:
        from nonebot import get_driver

        value = getattr(get_driver().config, name.lower(), None)
        if value not in (None, ""):
            return value
    except ValueError:  # NoneBot 还没初始化（单独跑测试时）
        pass
    return os.environ.get(name.upper())


SETTINGS_FILE = Path(os.environ.get("MYTEST_SETTINGS_FILE") or Path.cwd() / "data" / "settings" / "settings.json")
settings_store = SettingsStore(SETTINGS_FILE, env_lookup=_env_lookup)


def register(section: SettingSection) -> SectionView:
    return settings_store.register(section)


# 多个插件共用的大模型（骰娘点评、画图时把中文翻译成 tag）。
llm_settings = register(
    SettingSection(
        id="llm",
        title="大模型",
        description="OpenAI 兼容的对话接口。骰娘点评、画图时把中文描述翻译成 tag 都用它。",
        order=10,
        fields=[
            SettingField(
                "api_key",
                "API Key",
                type="secret",
                env=("dice_girl_ai_api_key", "ai_api_key", "openai_api_key"),
            ),
            SettingField(
                "base_url",
                "接口地址",
                default="https://api.openai.com/v1",
                help="填到 /v1 为止，例如 https://api.siliconflow.cn/v1",
                env=("dice_girl_ai_base_url", "ai_base_url", "openai_base_url"),
            ),
            SettingField(
                "model",
                "模型",
                default="gpt-4o-mini",
                help="保存 API Key 和接口地址后，点「获取列表」从接口读取可用模型",
                env=("dice_girl_ai_model", "ai_model", "openai_model"),
                fetchable=True,
            ),
            SettingField(
                "timeout",
                "超时（秒）",
                type="float",
                default=120.0,
                minimum=5,
                maximum=600,
                env=("dice_girl_ai_timeout",),
            ),
            SettingField(
                "max_tokens",
                "最大输出 token",
                type="int",
                default=5000,
                minimum=0,
                maximum=200000,
                help="单次回复最多生成多少 token（带思考的模型思考也算在内）。填 0 表示不限制，由接口决定",
            ),
            SettingField(
                "json_mode",
                "JSON 模式",
                type="bool",
                default=False,
                help="接口支持 response_format=json_object 时可以打开，输出更稳定",
                env=("dice_girl_ai_json_mode",),
            ),
            _proxy.mode_field(),
            _proxy.custom_field(),
        ],
    )
)

# 网络代理：公共代理和备用代理，各服务按自己的「连接方式」选用。
proxy_settings = register(_proxy.section())


def proxy_for(view: Any) -> str:
    """某个服务这次要用的代理（空字符串表示直连）。view 是带 proxy_mode / proxy 的配置分区；
    没有 proxy_mode 时按「跟以前一样」处理。"""
    mode = str(getattr(view, "proxy_mode", _proxy.AUTO) or _proxy.AUTO)
    custom = str(getattr(view, "proxy", "") or "")
    return _proxy.resolve(mode, custom, str(proxy_settings.main), str(proxy_settings.backup))


async def _test_proxies() -> str:
    lines = []
    for label, url in (("公共代理", str(proxy_settings.main)), ("备用代理", str(proxy_settings.backup)),
                       ("直连", "")):
        if label != "直连" and not url.strip():
            lines.append(f"{label}：没有填")
            continue
        lines.append(_proxy.report(label, url, await _proxy.probe(url)))
    return "\n".join(lines)


settings_store.set_tester("proxy", _test_proxies)
