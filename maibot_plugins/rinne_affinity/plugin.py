"""凛音好感度插件。

- 收到消息时：记下每条消息是谁发的（用来知道麦麦在回复谁）。
- 回复生成前：查出对方的好感度，把关系阶段、上班/下班状态、称呼追加到这次回复的要求里。
- 回复生成后：后台让模型判断这次互动让好感度变化多少（-max_delta ~ +max_delta）。

好感度按 QQ 号记录，私聊和所有群共享。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode

# ---------------------------------------------------------------- 配置


class GeneralSection(PluginConfigBase):
    __ui_label__ = "基础设置"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本（自动维护，请勿修改）")
    enabled: bool = Field(default=True, description="是否启用好感度")
    admin_ids: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 号：可以查看和设置别人的好感度",
    )
    default_favorability: int = Field(default=50, description="初次见面的好感度（0-100）")
    role_name: str = Field(default="白鸟凛音", description="角色名，用在提示词里")
    rules_file: str = Field(
        default="",
        description="关系规则文件路径（JSON）。留空使用插件内置规则；也可以填 NoneBot 骰娘插件的 personas.json 路径，两边共用一份规则",
        json_schema_extra={"placeholder": "/root/myTest/mytest/plugins/dice_girl_plugin/personas.json"},
    )
    rules_role_id: str = Field(default="rinne", description="规则文件是 personas.json 时，读取哪个角色")


class ScoringSection(PluginConfigBase):
    __ui_label__ = "好感度变化"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否根据互动自动调整好感度")
    max_delta: int = Field(default=3, description="单次互动最多加减多少")
    model: str = Field(default="", description="用来判断好感变化的模型名，留空使用默认模型")


class RinneAffinityConfig(PluginConfigBase):
    plugin: GeneralSection = Field(default_factory=GeneralSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)


# ---------------------------------------------------------------- 关系规则

DEFAULT_RULES: dict[str, Any] = {
    "stages": [
        {
            "range": [0, 30],
            "name": "陌生的客人",
            "user_call": "客人",
            "attitude": "公事公办的礼貌，笑容标准但没有温度，像在接待一位来访者。",
            "directive": "严格保持距离，只提供必要的服务；不主动关心，不开玩笑。",
        },
        {
            "range": [31, 59],
            "name": "服务对象",
            "user_call": "您",
            "attitude": "尽职周到，开始会用冷幽默吐槽对方的笨拙，但始终停留在工作范围内。",
            "directive": "保持专业，可以有礼貌的吐槽；被靠近时礼貌地后退一步。",
        },
        {
            "range": [60, 94],
            "name": "我的主人",
            "user_call": "主人",
            "off_duty_call": "直呼对方的名字",
            "attitude": "明显偏心这位主人，照顾得格外细致；下班后愿意在他面前放松一点，露出大小姐的任性和毒舌。",
            "directive": "工作中把偏爱藏在细节里；下班后可以捉弄、抱怨和轻微暧昧，但还不会叫对方“呆货”。",
        },
        {
            "range": [95, 100],
            "name": "唯一的主人",
            "user_call": "主人",
            "off_duty_call": "平时直呼名字；偶尔在对方犯傻、或者让她心软的时候叫一声“呆货”（不要每句都叫）",
            "attitude": "工作中依旧完美，但只有对这个人，递茶时会多停留一秒。下班后毫不掩饰地任性，带刺地撒娇、使唤对方，嘴上嫌弃，行动上却离不开。",
            "directive": "下班后允许亲近、占有欲和带刺的撒娇；仍保持骄傲，不变成无条件顺从。",
        },
    ],
    "duty": {
        "on": {
            "label": "工作中",
            "description": "专业、恭敬、完美的女仆。使用敬语，情绪收得很好，冷幽默藏在礼貌里。",
            "rules": [
                "全程使用敬语，称呼按上面给出的称呼。",
                "绝不使用“呆货”这个称呼，也不自称“本小姐”。",
                "对方用私下的语气逗她时，礼貌地提醒对方现在还在工作，每次换一种说法。",
            ],
        },
        "off": {
            "label": "下班后",
            "description": "卸下女仆身份的大小姐本人。说话直接、骄傲、爱捉弄人，只有对亲近的人才会露出这一面。",
            "rules": [
                "不用敬语，语气随意，带点居高临下的大小姐腔调。",
                "可以抱怨今天的工作，可以捉弄对方，但关心依旧是真的。",
                "如果对方用使唤女仆的方式命令她，可以回一句“已经下班了”，然后视心情决定帮不帮。",
            ],
        },
        "off_duty_min_favorability": 95,
        "night_off_duty_min_favorability": 60,
        "night_hours": [21, 6],
    },
}

# 上下班按北京时间判断，避免服务器使用 UTC 时“晚上”错开 8 小时。
BEIJING = timezone(timedelta(hours=8))
PROMPT_MARKER = "【好感度插件】"
# 凛音工具插件（rinne_tools）在回复前写入的聊天预设标记，它在本插件之前运行。
PRESET_MODES = re.compile(r"【聊天预设】生效模式：([a-z_,]+)")
NSFW_RELATION_RULE = (
    "成人模式开着：亲密程度和性内容按【聊天预设】里的要求来。上面的关系只决定称呼和你对这个人有多亲近，不限制亲密，也不要因为在工作中就拒绝。"
)
NSFW_SCORING_NOTE = (
    "\n这次对话开着成人模式：调情、性暗示和性方面的请求本身不算轻浮冒犯，不因此扣分；"
    "只按对方是否真诚、是否尊重她来评分。"
)


def load_rules(path: str, role_id: str) -> tuple[dict[str, Any], str]:
    """返回 (规则, 来源说明)。文件读不了或格式不对时退回内置规则。"""
    if not path.strip():
        return DEFAULT_RULES, "内置规则"
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, dict) and "roles" in data:
        data = data["roles"][role_id]
    if not isinstance(data, dict) or not data.get("stages") or not data.get("duty"):
        raise ValueError("规则文件里缺少 stages 或 duty")
    return data, path


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(value)))


def stage_for(rules: dict[str, Any], favorability: int) -> dict[str, Any]:
    stages = rules.get("stages") or []
    for stage in stages:
        low, high = stage["range"]
        if low <= favorability <= high:
            return stage
    return stages[0] if stages else {"name": "未知", "user_call": "您"}


def _in_hours(hour: int, span: list[int]) -> bool:
    start, end = span
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@dataclass
class DutyState:
    off_duty: bool
    label: str
    description: str
    rules: list[str]


def duty_state(rules: dict[str, Any], is_group: bool, favorability: int, now: datetime | None = None) -> DutyState:
    """群聊永远上班；私聊里好感度达到门槛下班，夜间（北京时间）门槛更低。"""
    duty = rules.get("duty") or {}
    off = False
    if not is_group:
        hour = (now or datetime.now(BEIJING)).astimezone(BEIJING).hour
        threshold = int(duty.get("off_duty_min_favorability", 101))
        if _in_hours(hour, duty.get("night_hours", [21, 6])):
            threshold = min(threshold, int(duty.get("night_off_duty_min_favorability", threshold)))
        off = favorability >= threshold
    info = duty.get("off" if off else "on") or {}
    return DutyState(
        off_duty=off,
        label=str(info.get("label") or ("下班后" if off else "工作中")),
        description=str(info.get("description") or ""),
        rules=[str(rule) for rule in info.get("rules") or []],
    )


def user_call_for(stage: dict[str, Any], state: DutyState) -> str:
    if state.off_duty and stage.get("off_duty_call"):
        return str(stage["off_duty_call"])
    return str(stage.get("user_call") or "您")


def preset_modes(extra_prompt: str) -> set[str]:
    """凛音工具插件在回复要求里写的「生效模式」。"""
    match = PRESET_MODES.search(extra_prompt or "")
    return set(match.group(1).split(",")) if match else set()


def build_extra_prompt(name: str, favorability: int, stage: dict[str, Any], state: DutyState,
                       *, nsfw: bool = False) -> str:
    if nsfw:
        # 成人模式下亲密程度交给聊天预设，关系只决定称呼和亲疏；阶段和上下班里限制亲密的要求都不加。
        items = [
            ("关系阶段", f"{stage.get('name', '未知')}（好感度 {favorability}/100）"),
            ("你现在的状态", state.label),
            ("称呼对方", user_call_for(stage, state)),
        ]
        rules = [NSFW_RELATION_RULE]
    else:
        items = [
            ("关系阶段", f"{stage.get('name', '未知')}（好感度 {favorability}/100）"),
            ("你现在的状态", f"{state.label}。{state.description}".rstrip("。") + "。"),
            ("称呼对方", user_call_for(stage, state)),
            ("态度", str(stage.get("attitude") or "")),
            ("要求", str(stage.get("directive") or "")),
        ]
        rules = state.rules
    lines = [f"{PROMPT_MARKER}你和这次回复的对象「{name}」的关系（以此为准）："]
    lines += [f"- {label}：{value}" for label, value in items if value.strip()]
    lines += [f"- {rule}" for rule in rules if rule.strip()]
    lines.append("- 开头直接回应对方说的内容，不要用“恕我直言”“遵命”之类的固定开场白，也不要和你最近几条回复用同样的开头。")
    lines.append("- 不要在回复里提到好感度、关系阶段这类后台信息。")
    return "\n".join(lines)


def parse_delta(text: str, max_delta: int) -> tuple[int, str]:
    """从模型输出里取出 {"delta": 整数, "reason": "..."}；解析不了就当作不变。"""
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return 0, "无法解析"
    try:
        data = json.loads(match.group(0))
        delta = int(data.get("delta", 0))
    except (ValueError, TypeError, AttributeError):
        return 0, "无法解析"
    reason = str(data.get("reason") or "").strip()[:40]
    return max(-max_delta, min(max_delta, delta)), reason


# ---------------------------------------------------------------- 消息解析


@dataclass
class Sender:
    user_id: str
    name: str
    is_group: bool
    text: str
    bot_id: str

    @property
    def is_bot(self) -> bool:
        return bool(self.bot_id) and self.user_id == self.bot_id


def _unwrap_message(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if "message_info" in value:
        return value
    for key in ("message", "data", "result"):
        inner = value.get(key)
        if isinstance(inner, dict) and "message_info" in inner:
            return inner
    return None


def extract_sender(message: Any) -> Sender | None:
    message = _unwrap_message(message)
    if message is None:
        return None
    info = message.get("message_info") or {}
    user = info.get("user_info") or {}
    user_id = str(user.get("user_id") or "").strip()
    if not user_id:
        return None
    group = info.get("group_info")
    group_id = str(group.get("group_id") or "").strip() if isinstance(group, dict) else ""
    name = user.get("user_cardname") or user.get("user_nickname") or user_id
    return Sender(
        user_id=user_id,
        name=str(name),
        is_group=bool(group_id),
        text=str(message.get("processed_plain_text") or ""),
        bot_id=str(message.get("account_id") or ""),
    )


# ---------------------------------------------------------------- 存储


class AffinityStore:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS affinity (
                    user_id TEXT PRIMARY KEY,
                    favorability INTEGER NOT NULL,
                    interactions INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS affinity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    old_value INTEGER NOT NULL,
                    new_value INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_affinity_log_user ON affinity_log(user_id, id)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, user_id: str, default: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT favorability FROM affinity WHERE user_id = ?", (user_id,)).fetchone()
        return int(row[0]) if row else clamp(default)

    def detail(self, user_id: str, default: int) -> tuple[int, int, str]:
        """(好感度, 互动次数, 最近一次变化的原因)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT favorability, interactions FROM affinity WHERE user_id = ?", (user_id,)
            ).fetchone()
            log = conn.execute(
                "SELECT old_value, new_value, reason FROM affinity_log WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        favorability, interactions = (int(row[0]), int(row[1])) if row else (clamp(default), 0)
        last = ""
        if log:
            change = log[1] - log[0]
            last = f"{'+' if change >= 0 else ''}{change}，{log[2] or '无说明'}"
        return favorability, interactions, last

    def change(self, user_id: str, *, default: int, delta: int = 0, value: int | None = None,
               reason: str = "", source: str, count_interaction: bool = False) -> tuple[int, int]:
        """加减（delta）或直接设置（value），返回 (旧值, 新值)。"""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT favorability FROM affinity WHERE user_id = ?", (user_id,)).fetchone()
            old = int(row[0]) if row else clamp(default)
            new = clamp(value if value is not None else old + delta)
            conn.execute(
                """
                INSERT INTO affinity (user_id, favorability, interactions, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    favorability = excluded.favorability,
                    interactions = interactions + ?,
                    updated_at = excluded.updated_at
                """,
                (user_id, new, int(count_interaction), now, int(count_interaction)),
            )
            if new != old or value is not None:
                conn.execute(
                    "INSERT INTO affinity_log (user_id, old_value, new_value, reason, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, old, new, reason, source, now),
                )
        return old, new


# ---------------------------------------------------------------- 插件

SCORING_SYSTEM_PROMPT = """你是关系变化评估器。角色「{role}」是一位公私分明、骄傲但真诚的大小姐，主动来当对方的女仆。
根据下面这一次互动，判断她对这位用户的好感应该怎么变化。

评分标准：
- +1 ~ +2：真诚的关心、感谢、尊重她，有趣或用心的互动，记得她说过的事。
- +3：特别打动她的举动，很少出现。
- 0：普通闲聊、提问、事务性对话，以及无法判断的情况。大多数互动都是 0 或 +1。
- -1 ~ -2：轻浮冒犯、把她当工具随意使唤、无理纠缠。
- -3：辱骂、恶意攻击。
只看用户这一次的言行，不要因为她的回复本身语气冷淡就扣分。

只返回一个 JSON 对象，不要输出其他内容：
{{"delta": 整数, "reason": "不超过20字的原因"}}"""


class RinneAffinityPlugin(MaiBotPlugin):
    config_model = RinneAffinityConfig

    MAX_CACHED_MESSAGES = 3000
    MAX_TRACKED_REPLIES = 500

    async def on_load(self) -> None:
        self._senders: OrderedDict[str, Sender] = OrderedDict()
        self._pending: OrderedDict[str, Sender] = OrderedDict()
        self._scored: OrderedDict[str, None] = OrderedDict()
        self._unscored: OrderedDict[str, None] = OrderedDict()  # 强攻击力模式下的回复
        self._nsfw_replies: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._store = AffinityStore(data_dir / "affinity.db")
        self._reload_rules()
        self.ctx.logger.info("[好感度] 已加载，数据库 %s", data_dir / "affinity.db")

    async def on_unload(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.ctx.logger.info("[好感度] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._reload_rules()

    # -------------------------------------------------------- 内部工具

    def _cfg(self) -> RinneAffinityConfig:
        try:
            return self.config  # type: ignore[return-value]
        except RuntimeError:
            return RinneAffinityConfig()

    def _reload_rules(self) -> None:
        general = self._cfg().plugin
        try:
            self._rules, source = load_rules(general.rules_file, general.rules_role_id)
        except Exception as e:
            self._rules, source = DEFAULT_RULES, "内置规则"
            self.ctx.logger.warning("[好感度] 读取规则文件失败，改用内置规则: %s", e)
        self.ctx.logger.info("[好感度] 使用%s", source)

    @staticmethod
    def _remember(cache: OrderedDict, key: str, value: Any, limit: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    async def _resolve_sender(self, message_id: str, session_id: str) -> Sender | None:
        if not message_id:
            return None
        sender = self._senders.get(message_id)
        if sender is not None:
            return sender
        try:
            found = await self.ctx.message.get_by_id(message_id, stream_id=session_id or "")
        except Exception as e:
            self.ctx.logger.debug("[好感度] 查询消息 %s 失败: %s", message_id, e)
            return None
        sender = extract_sender(found)
        if sender is not None:
            self._remember(self._senders, message_id, sender, self.MAX_CACHED_MESSAGES)
        return sender

    def _is_admin(self, user_id: str) -> bool:
        return user_id in {str(i).strip() for i in self._cfg().plugin.admin_ids}

    # -------------------------------------------------------- Hook

    @HookHandler("chat.receive.after_process", name="remember_sender", mode=HookMode.BLOCKING,
                 error_policy=ErrorPolicy.SKIP)
    async def remember_sender(self, **kwargs: Any) -> dict[str, Any]:
        try:
            message = kwargs.get("message")
            sender = extract_sender(message)
            message_id = str((message or {}).get("message_id") or "") if isinstance(message, dict) else ""
            if sender is not None and message_id:
                self._remember(self._senders, message_id, sender, self.MAX_CACHED_MESSAGES)
        except Exception as e:
            self.ctx.logger.warning("[好感度] 记录消息发送者失败: %s", e)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_request", name="inject_relationship", mode=HookMode.BLOCKING,
                 error_policy=ErrorPolicy.SKIP)
    async def inject_relationship(self, **kwargs: Any) -> dict[str, Any]:
        try:
            cfg = self._cfg()
            if not cfg.plugin.enabled:
                return {"action": "continue"}
            reply_id = str(kwargs.get("reply_message_id") or "")
            sender = await self._resolve_sender(reply_id, str(kwargs.get("session_id") or ""))
            if sender is None or sender.is_bot:
                return {"action": "continue"}

            existing = str(kwargs.get("extra_prompt") or "")
            if PROMPT_MARKER in existing:
                return {"action": "continue"}
            modes = preset_modes(existing)
            if "aggressive" in modes:
                # 强攻击力模式：这次回复不加关系信息，也不评分
                self._remember(self._unscored, reply_id, None, self.MAX_TRACKED_REPLIES)
                return {"action": "continue"}
            nsfw = "nsfw" in modes
            if nsfw:
                self._remember(self._nsfw_replies, reply_id, None, self.MAX_TRACKED_REPLIES)

            favorability = self._store.get(sender.user_id, cfg.plugin.default_favorability)
            stage = stage_for(self._rules, favorability)
            state = duty_state(self._rules, sender.is_group, favorability)
            extra = build_extra_prompt(sender.name, favorability, stage, state, nsfw=nsfw)
            kwargs["extra_prompt"] = f"{existing}\n\n{extra}".strip()
            self._remember(self._pending, reply_id, sender, self.MAX_TRACKED_REPLIES)
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as e:
            self.ctx.logger.warning("[好感度] 注入关系状态失败: %s", e)
            return {"action": "continue"}

    @HookHandler("maisaka.replyer.after_response", name="schedule_scoring", mode=HookMode.BLOCKING,
                 error_policy=ErrorPolicy.SKIP)
    async def schedule_scoring(self, **kwargs: Any) -> dict[str, Any]:
        try:
            cfg = self._cfg()
            reply_id = str(kwargs.get("reply_message_id") or "")
            response = str(kwargs.get("response") or "").strip()
            if not (cfg.plugin.enabled and cfg.scoring.enabled and reply_id and response):
                return {"action": "continue"}
            if reply_id in self._scored or reply_id in self._unscored:
                return {"action": "continue"}
            sender = self._pending.pop(reply_id, None) or await self._resolve_sender(
                reply_id, str(kwargs.get("session_id") or "")
            )
            if sender is None or sender.is_bot:
                return {"action": "continue"}
            self._remember(self._scored, reply_id, None, self.MAX_TRACKED_REPLIES)
            nsfw = reply_id in self._nsfw_replies
            task = asyncio.get_running_loop().create_task(self._score(sender, response, nsfw=nsfw))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as e:
            self.ctx.logger.warning("[好感度] 安排好感度评估失败: %s", e)
        return {"action": "continue"}

    async def _score(self, sender: Sender, response: str, *, nsfw: bool = False) -> None:
        cfg = self._cfg()
        try:
            favorability = self._store.get(sender.user_id, cfg.plugin.default_favorability)
            stage = stage_for(self._rules, favorability)
            system = SCORING_SYSTEM_PROMPT.format(role=cfg.plugin.role_name) + (NSFW_SCORING_NOTE if nsfw else "")
            prompt = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"当前好感度：{favorability}（{stage.get('name', '')}）\n"
                        f"用户「{sender.name}」说：{sender.text[:500] or '（非文字消息）'}\n"
                        f"{cfg.plugin.role_name}回复：{response[:500]}"
                    ),
                },
            ]
            result = await self.ctx.llm.generate(
                prompt=prompt, model=cfg.scoring.model, temperature=0.2, max_tokens=200
            )
            if not result.get("success"):
                self.ctx.logger.warning("[好感度] 评估模型调用失败: %s", result)
                return
            delta, reason = parse_delta(str(result.get("response") or ""), max(0, cfg.scoring.max_delta))
            old, new = self._store.change(
                sender.user_id,
                default=cfg.plugin.default_favorability,
                delta=delta,
                reason=reason,
                source="chat",
                count_interaction=True,
            )
            if old != new:
                self.ctx.logger.info(
                    "[好感度] %s(%s) %d -> %d（%+d）：%s", sender.name, sender.user_id, old, new, new - old, reason
                )
        except Exception as e:
            self.ctx.logger.warning("[好感度] 评估失败: %s", e)

    # -------------------------------------------------------- 命令

    async def _reply(self, stream_id: str, text: str) -> tuple[bool, str, int]:
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command("affinity_view", description="查看好感度：/好感度 [QQ号]",
             pattern=r"^/好感度(?:\s+(?P<target>\d{5,12}))?\s*$")
    async def cmd_view(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        sender = extract_sender(kwargs.get("message"))
        if sender is None:
            return await self._reply(stream_id, "无法识别发送者。")
        target = (kwargs.get("matched_groups") or {}).get("target") or sender.user_id
        if target != sender.user_id and not self._is_admin(sender.user_id):
            return await self._reply(stream_id, "只能查看自己的好感度。")
        cfg = self._cfg()
        favorability, interactions, last = self._store.detail(target, cfg.plugin.default_favorability)
        stage = stage_for(self._rules, favorability)
        who = "你" if target == sender.user_id else target
        text = f"{cfg.plugin.role_name}对{who}的好感度：{favorability}（{stage.get('name', '')}）\n互动次数：{interactions}"
        if last:
            text += f"\n最近一次变化：{last}"
        return await self._reply(stream_id, text)

    @Command("affinity_set", description="设置好感度（管理员）：/设置好感度 QQ号 数值",
             pattern=r"^/设置好感度\s+(?P<target>\d{5,12})\s+(?P<value>-?\d{1,3})\s*$")
    async def cmd_set(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        sender = extract_sender(kwargs.get("message"))
        if sender is None:
            return await self._reply(stream_id, "无法识别发送者。")
        if not self._cfg().plugin.admin_ids:
            return await self._reply(stream_id, "还没有设置管理员，请先在插件配置的 admin_ids 里填写 QQ 号。")
        if not self._is_admin(sender.user_id):
            return await self._reply(stream_id, "只有管理员可以设置好感度。")
        groups = kwargs.get("matched_groups") or {}
        target, value = str(groups.get("target")), int(groups.get("value"))
        old, new = self._store.change(
            target,
            default=self._cfg().plugin.default_favorability,
            value=value,
            reason=f"管理员 {sender.user_id} 手动设置",
            source="admin",
        )
        stage = stage_for(self._rules, new)
        return await self._reply(stream_id, f"已将 {target} 的好感度从 {old} 设为 {new}（{stage.get('name', '')}）。")


def create_plugin():
    return RinneAffinityPlugin()
