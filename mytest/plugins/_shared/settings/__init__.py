"""全局配置中心。

各插件在这里注册自己的配置分区，WebUI 负责展示和修改，插件每次使用时读取最新值，改完不用重启。
配置保存在 MYTEST_SETTINGS_FILE（默认 data/settings/settings.json），没有在 WebUI 里改过的项
仍然读取 .env / 环境变量，所以原来的 .env 配置继续有效。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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
from .. import proxy as _proxy

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


def proxy_for(view: Any, parent: Any = None) -> str:
    """某个服务这次要用的代理（空字符串表示直连）。view 是带 proxy_mode / proxy 的配置分区；
    没有 proxy_mode 时按「跟以前一样」处理。parent：view 选了「跟上级一样」时用谁的设置。"""
    mode = str(getattr(view, "proxy_mode", _proxy.AUTO) or _proxy.AUTO)
    if mode == _proxy.INHERIT:
        if parent is not None:
            return proxy_for(parent)
        mode = _proxy.AUTO
    custom = str(getattr(view, "proxy", "") or "")
    return _proxy.resolve(mode, custom, str(proxy_settings.main), str(proxy_settings.backup))


async def _test_proxies() -> str:
    lines = []
    env = _proxy.environment()
    if env:
        shown = "，".join(f"{k}={v}" for k, v in env.items())
        lines.append(f"容器环境变量里的代理：{shown}。「跟以前一样」且没填代理的服务会走它，选「直连」的不会")
    for label, url in (("公共代理", str(proxy_settings.main)), ("备用代理", str(proxy_settings.backup)),
                       ("直连", _proxy.NO_PROXY)):
        if label != "直连" and not url.strip():
            lines.append(f"{label}：没有填")
            continue
        lines.append(_proxy.report(label, url, await _proxy.probe(url)))
    return "\n".join(lines)


settings_store.set_tester("proxy", _test_proxies)


# ---------------------------------------------------------------- mihomo：节点和订阅


def _mihomo():
    from ..mihomo import Mihomo

    return Mihomo(str(proxy_settings.mihomo_url), str(proxy_settings.mihomo_secret))


def _expire_text(ts: int) -> str:
    import time

    return time.strftime("%Y-%m-%d", time.localtime(ts))


async def _mihomo_items() -> dict[str, Any]:
    from .. import mihomo

    if not str(proxy_settings.mihomo_url).strip():
        return {"items": [], "notice": "在上面填 mihomo 控制接口的地址并保存后，这里会列出代理组和订阅"}
    snap = await _mihomo().snapshot()
    rows = mihomo.items(snap, _expire_text)
    notice = "" if any(p.vehicle == "HTTP" for p in snap.providers) else \
        "mihomo 配置里没有订阅（proxy-providers），只能切换节点。要更新订阅，先按 docs/mihomo.md 把订阅写进配置"
    return {"items": rows, "notice": notice}


async def _mihomo_run(action: str, item_id: str, value: Any) -> str:
    from .. import mihomo

    client = _mihomo()
    kind, _, name = item_id.partition(":")
    if action == "test_all":
        snap = await client.snapshot()
        lines = []
        for g in snap.groups:
            if g.manual:
                result = await client.test_group(g.name)
                lines.append(f"{g.name}：{mihomo.delay_counts(g, snap, result)}")
        return "\n".join(lines) or "没有可以测速的代理组"
    if kind == "g":
        snap = await client.snapshot()
        group = next((g for g in snap.groups if g.name == name), None)
        if group is None:
            raise ValueError("这个代理组不在了，刷新一下")
        if action == "test":
            result = await client.test_group(name)
            best = sorted(((n, d) for n, d in result.items() if n in snap.nodes), key=lambda kv: kv[1])[:3]
            tip = "；最快：" + "、".join(f"{n} {d} ms" for n, d in best) if best else ""
            return f"测完了：{mihomo.delay_counts(group, snap, result)}{tip}"
        if action == "select":
            if not group.manual:
                raise ValueError(f"「{name}」是自动选择的组，由 mihomo 按延迟自己选，不能手动切换")
            node = mihomo.pick_node(value, group)
            await client.select(name, node)
            return f"「{name}」已切换到「{node}」，马上生效"
    if kind == "p" and action == "update":
        await client.update_provider(name)
        return f"订阅「{name}」已更新"
    raise ValueError("这个按钮对这一行没有用")


async def _test_mihomo_and_proxies() -> str:
    lines = []
    if str(proxy_settings.mihomo_url).strip():
        try:
            lines.append(f"mihomo 控制接口：连上了（{await _mihomo().version()}）")
        except Exception as e:
            from ..services.http import describe_exception

            lines.append(f"mihomo 控制接口：{describe_exception('mihomo', e)}")
    return "\n".join([*lines, await _test_proxies()])


settings_store.set_tester("proxy", _test_mihomo_and_proxies)
settings_store.set_manager("proxy", ItemManager(
    title="节点和订阅",
    help="来自 mihomo 的控制接口。切换节点、更新订阅都是马上生效，不用点下面的保存；mihomo 重启后会回到配置文件里的默认节点",
    items=_mihomo_items,
    actions=(
        ManagerAction("test_all", "全部测速", scope="global", primary=True),
        ManagerAction("select", "切换节点", input="select"),
        ManagerAction("test", "测速"),
        ManagerAction("update", "立即更新"),
    ),
    run=_mihomo_run,
))
