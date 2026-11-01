"""不接指令：发给 NoneBot 的指令（/画图、.rd、搜图……），MaiBot 不处理，凛音就不会对着指令接话。

在 chat.receive.before_process（消息进入 MaiBot 之前）用阻塞处理器返回 abort，这条消息就不会进入聊天流程，
也不会记进 MaiBot 的聊天记录。MaiBot 自己的指令（/好感度、/clear 等）写在 keep_prefixes 里，照常处理。

有些指令会接着问问题（/画图 -多图 要你一张张发图、/点歌 要你回序号），所以同一个人发了指令之后，
在同一个群（或私聊）里 follow_up_seconds 秒内的消息也不处理。

先用「只记录」模式部署，确认没有误拦，再打开拦截：
- mode = "observe"：不拦截，只在日志和 skipped.jsonl 里记下哪些消息会被拦（默认）
- mode = "block"：拦截
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

MAX_RECORDS = 200

# NoneBot 那边的指令长什么样：
# - 以 / 开头：/画图、/nai、/点歌、/说、/帮助、/p站 每日一图、/dice 投骰、跑团指令……
# - 以 . 或 。 加字母开头：.rd、.ra、.sc、.st（「。。。」这种省略号不算）
# - Pixiv 不带 / 也能用：搜图、最新、美图、热门、每日一图，前面可以加「p站」
DEFAULT_PATTERNS = [
    r"^[/／]\S",
    r"^[.。][A-Za-z]",
    r"^(?:[/／]?p站\s+)?(?:搜图|最新|美图|热门)(?:\s|$)",
    r"^(?:[/／]?p站\s+)?每日一图$",
    r"^p站\s",
]
# MaiBot 自己的指令，不能拦
DEFAULT_KEEP_PREFIXES = ["/好感度", "/设置好感度", "/clear"]

# ---------------------------------------------------------------- 配置


class GeneralSection(PluginConfigBase):
    __ui_label__ = "不接指令"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本（自动维护，请勿修改）")
    mode: str = Field(
        default="observe",
        description="observe：只记录不拦截（先用这个确认没有误拦）；block：指令消息 MaiBot 不处理",
        json_schema_extra={"enum": ["observe", "block"]},
    )
    patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PATTERNS),
        description="哪些消息算指令：正则表达式，每项一个，匹配去掉开头的回复和 @ 之后的文字",
    )
    keep_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_KEEP_PREFIXES),
        description="MaiBot 自己的指令，以这些开头的消息不拦",
    )
    follow_up_seconds: int = Field(
        default=120,
        description="发了指令之后，同一个人在同一个聊天里多少秒内的消息也不处理（发图、回序号等）；0 表示不拦后续消息",
    )


class RinneCommandGuardConfig(PluginConfigBase):
    plugin: GeneralSection = Field(default_factory=GeneralSection)


# ---------------------------------------------------------------- 判断（不依赖 SDK 运行时，方便测试）

_LEADING_AT = re.compile(r"^(?:@\S+\s*)+")


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


def message_of(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    return _unwrap(kwargs.get("message")) or _unwrap(kwargs)


def text_of(message: dict[str, Any]) -> str:
    """消息里用户打的字：只取文字段，跳过回复、@、图片；开头的「@某人」也去掉。"""
    raw = message.get("raw_message")
    if isinstance(raw, list):
        text = "".join(str(seg.get("data") or "") for seg in raw
                       if isinstance(seg, dict) and seg.get("type") == "text")
    else:
        text = str(message.get("processed_plain_text") or "")
    return _LEADING_AT.sub("", text.strip()).strip()


def chat_key(message: dict[str, Any]) -> tuple[str, str] | None:
    """（聊天，发消息的人）。群聊按群号，私聊按对方 QQ。认不出来时返回 None。"""
    info = message.get("message_info") or {}
    user = str((info.get("user_info") or {}).get("user_id") or "").strip()
    if not user:
        return None
    group = info.get("group_info")
    group_id = str(group.get("group_id") or "").strip() if isinstance(group, dict) else ""
    return (f"group:{group_id}" if group_id else f"private:{user}", user)


@lru_cache(maxsize=8)
def _compile(patterns: tuple[str, ...]) -> tuple[list[re.Pattern[str]], list[str]]:
    """编译正则；写错的跳过并返回出来，免得一条写错就整个插件不工作。"""
    compiled, bad = [], []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            bad.append(p)
    return compiled, bad


def is_command(text: str, patterns: list[str], keep_prefixes: list[str]) -> bool:
    if not text or any(text.startswith(k) for k in keep_prefixes if k):
        return False
    compiled, _ = _compile(tuple(patterns))
    return any(p.search(text) for p in compiled)


class FollowUps:
    """记住谁刚发过指令：(聊天, 人) → 截止时间。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until: dict[tuple[str, str], float] = {}

    def mark(self, key: tuple[str, str], seconds: float) -> None:
        if seconds > 0:
            self._until[key] = self._clock() + seconds

    def active(self, key: tuple[str, str]) -> bool:
        now = self._clock()
        for k in [k for k, t in self._until.items() if t <= now]:
            del self._until[k]
        return key in self._until


def decide(kwargs: dict[str, Any], cfg: GeneralSection, follow_ups: FollowUps) -> tuple[str, str]:
    """返回（原因，文字）。原因是「指令」「指令后续」或空字符串（不拦）。"""
    message = message_of(kwargs)
    if message is None:
        return "", ""
    text = text_of(message)
    key = chat_key(message)
    if is_command(text, list(cfg.patterns), list(cfg.keep_prefixes)):
        if key is not None:
            follow_ups.mark(key, float(cfg.follow_up_seconds))
        return "指令", text
    if key is not None and follow_ups.active(key) and not any(text.startswith(k) for k in cfg.keep_prefixes if k):
        return "指令后续", text
    return "", text


# ---------------------------------------------------------------- 插件


class RinneCommandGuardPlugin(MaiBotPlugin):
    config_model = RinneCommandGuardConfig

    async def on_load(self) -> None:
        self._records = 0
        self._file: Path | None = None
        self._follow_ups = FollowUps()
        try:
            data_dir = Path(self.ctx.paths.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            self._file = data_dir / "skipped.jsonl"
        except Exception as e:
            self.ctx.logger.warning("[不接指令] 拿不到数据目录，只写日志: %s", e)
        self._announce()

    async def on_unload(self) -> None:
        self.ctx.logger.info("[不接指令] 已卸载")

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
        action = "拦截" if cfg.mode == "block" else "只记录不拦截"
        self.ctx.logger.info("[不接指令] 模式：%s，指令规则 %d 条，后续消息 %d 秒",
                             action, len(cfg.patterns), int(cfg.follow_up_seconds))
        _, bad = _compile(tuple(cfg.patterns))
        for p in bad:
            self.ctx.logger.warning("[不接指令] 这条规则写错了，已跳过：%s", p)

    def _record(self, reason: str, text: str, blocked: bool, kwargs: dict[str, Any]) -> None:
        self.ctx.logger.info("[不接指令] %s：%s%s", reason, text[:30], "（已拦截）" if blocked else "（只记录，没有拦截）")
        if self._file is None or self._records >= MAX_RECORDS:
            return
        self._records += 1
        message = message_of(kwargs) or {}
        key = chat_key(message)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chat": key[0] if key else "",
            "reason": reason,
            "blocked": blocked,
            "text": text[:50],
        }
        try:
            with self._file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.ctx.logger.warning("[不接指令] 写记录失败: %s", e)

    @HookHandler(
        "chat.receive.before_process",
        name="command_guard",
        description="NoneBot 的指令和指令后续的消息不进入 MaiBot",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        # 自己出错时放行，宁可多回一句，也不能把正常聊天吞掉
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard(self, **kwargs: Any) -> dict[str, Any]:
        try:
            cfg = self._cfg()
            reason, text = decide(kwargs, cfg, self._follow_ups)
            if not reason:
                return {"action": "continue"}
            blocked = cfg.mode == "block"
            self._record(reason, text, blocked, kwargs)
            return {"action": "abort"} if blocked else {"action": "continue"}
        except Exception as e:
            self.ctx.logger.warning("[不接指令] 判断失败，放行: %s", e)
            return {"action": "continue"}


def create_plugin():
    return RinneCommandGuardPlugin()
