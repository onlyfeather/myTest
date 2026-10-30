"""跑团群静默：跑团用的群里，MaiBot 不处理任何消息，全部交给 NoneBot 的跑团插件（凛音当 KP）。

在 chat.receive.before_process（消息进入 MaiBot 之前）用阻塞处理器返回 abort，这条消息就不会进入聊天流程。

先用「只记录」模式部署，确认在你的 MaiBot 版本上拦截点和消息结构都对，再打开拦截：
- mode = "observe"：不拦截，只在日志和 guard.jsonl 里记下跑团群的消息会被拦（默认）
- mode = "block"：拦截
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

MAX_RECORDS = 200

# ---------------------------------------------------------------- 配置


class GeneralSection(PluginConfigBase):
    __ui_label__ = "跑团群"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本（自动维护，请勿修改）")
    mode: str = Field(
        default="observe",
        description="observe：只记录不拦截（先用这个确认没问题）；block：跑团群里 MaiBot 不处理任何消息",
        json_schema_extra={"enum": ["observe", "block"]},
    )
    groups: list[str] = Field(default_factory=list, description="跑团群的群号，每项一个。只填专门用来跑团的群")


class RinneTrpgGuardConfig(PluginConfigBase):
    plugin: GeneralSection = Field(default_factory=GeneralSection)


# ---------------------------------------------------------------- 判断（不依赖 SDK 运行时，方便测试）


def _unwrap(value: Any) -> dict[str, Any] | None:
    """Hook 参数里的消息可能直接给，也可能包在 message / data 里。"""
    if not isinstance(value, dict):
        return None
    if "message_info" in value:
        return value
    for key in ("message", "data", "result"):
        inner = value.get(key)
        if isinstance(inner, dict) and "message_info" in inner:
            return inner
    return None


def group_of(kwargs: dict[str, Any]) -> str:
    """消息所在的群号；私聊或认不出来时返回空字符串。"""
    message = _unwrap(kwargs.get("message")) or _unwrap(kwargs)
    if message is None:
        return ""
    group = (message.get("message_info") or {}).get("group_info")
    return str(group.get("group_id") or "").strip() if isinstance(group, dict) else ""


def should_block(kwargs: dict[str, Any], groups: list[str]) -> str:
    """要拦的话返回群号，否则返回空字符串。"""
    group_id = group_of(kwargs)
    wanted = {str(g).strip() for g in groups if str(g).strip()}
    return group_id if group_id and group_id in wanted else ""


# ---------------------------------------------------------------- 插件


class RinneTrpgGuardPlugin(MaiBotPlugin):
    config_model = RinneTrpgGuardConfig

    async def on_load(self) -> None:
        self._records = 0
        self._file: Path | None = None
        try:
            data_dir = Path(self.ctx.paths.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            self._file = data_dir / "guard.jsonl"
        except Exception as e:
            self.ctx.logger.warning("[跑团群静默] 拿不到数据目录，只写日志: %s", e)
        self._announce()

    async def on_unload(self) -> None:
        self.ctx.logger.info("[跑团群静默] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._announce()

    def _cfg(self) -> GeneralSection:
        try:
            return self.config.plugin  # type: ignore[attr-defined]
        except RuntimeError:
            return GeneralSection()

    def _announce(self) -> None:
        cfg = self._cfg()
        groups = "、".join(cfg.groups) or "（没有填）"
        action = "拦截" if cfg.mode == "block" else "只记录不拦截"
        self.ctx.logger.info("[跑团群静默] 模式：%s，跑团群：%s", action, groups)

    def _record(self, group_id: str, blocked: bool, kwargs: dict[str, Any]) -> None:
        self.ctx.logger.info("[跑团群静默] 群 %s 的消息%s", group_id, "已拦截" if blocked else "（只记录，没有拦截）")
        if self._file is None or self._records >= MAX_RECORDS:
            return
        self._records += 1
        message = _unwrap(kwargs.get("message")) or {}
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "group": group_id,
            "blocked": blocked,
            "keys": sorted(str(k) for k in kwargs),
            "text": str(message.get("processed_plain_text") or "")[:50],
        }
        try:
            with self._file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.ctx.logger.warning("[跑团群静默] 写记录失败: %s", e)

    @HookHandler(
        "chat.receive.before_process",
        name="trpg_group_guard",
        description="跑团群里的消息不进入 MaiBot",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        # 自己出错时放行，宁可多回一句，也不能把所有群的消息都吞掉
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard(self, **kwargs: Any) -> dict[str, Any]:
        try:
            cfg = self._cfg()
            group_id = should_block(kwargs, list(cfg.groups))
            if not group_id:
                return {"action": "continue"}
            blocked = cfg.mode == "block"
            self._record(group_id, blocked, kwargs)
            return {"action": "abort"} if blocked else {"action": "continue"}
        except Exception as e:
            self.ctx.logger.warning("[跑团群静默] 判断失败，放行: %s", e)
            return {"action": "continue"}


def create_plugin():
    return RinneTrpgGuardPlugin()
