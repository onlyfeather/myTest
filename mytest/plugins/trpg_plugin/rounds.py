"""每一轮的行动收集：区分角色行动和场外、记录、合并、改成场外、超时。不依赖 NoneBot。

一轮的阶段：
- collecting：收集中。在场的人每人至少行动一次，并且安静 settle_delay 秒（让人补充第二条），就收齐。
  第一个人行动后 ask_after 还有人没动，问一次「还有人要行动吗」；问完 standby_after 没回应，
  没动的人按原地待命，直接收齐。
- collected：收齐了，交给结算。结算开始后再收到的行动算下一轮的。
- done：结算完了。

时间都由调用方传进来（now），后台循环每隔一会儿调用 tick()。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .campaign import JOINING, PRESENT, RUNNING, Campaign, Campaigns

COLLECTING, COLLECTED, DONE = "collecting", "collected", "done"

MAX_ACTION_CHARS = 800  # 一条行动最多记多少字
MAX_ACTIONS_PER_ROUND = 10  # 一个人一轮最多记几条
OOC_REPLY_WORDS = {"场外", "算场外", "这是场外", "这条场外"}
COMMAND_PREFIXES = ("/", ".", "。", "!", "！")

_OPEN = "（("
_CLOSE = "）)"
_KP_QUESTION = re.compile(r"^\s*kp\s*[:：,，]?\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Timing:
    ask_after: int = 30 * 60
    standby_after: int = 12 * 3600
    settle_delay: int = 60


@dataclass(frozen=True)
class Classified:
    action: str = ""  # 角色行动（去掉了括号里的场外部分）
    questions: tuple[str, ...] = ()  # （KP …）场外提问


def classify(text: str) -> Classified:
    """括号里的是场外，（KP …）是问 KP 的场外问题。没写右括号时，左括号后面全算场外。"""
    text = (text or "").strip()
    if not text or text.startswith(COMMAND_PREFIXES):
        return Classified()
    action: list[str] = []
    questions: list[str] = []
    depth = 0
    inner: list[str] = []
    for ch in text:
        if ch in _OPEN:
            if depth == 0:
                inner = []
            else:
                inner.append(ch)
            depth += 1
        elif ch in _CLOSE and depth:
            depth -= 1
            if depth == 0:
                _take_ooc("".join(inner), questions)
            else:
                inner.append(ch)
        elif depth:
            inner.append(ch)
        else:
            action.append(ch)
    if depth:
        _take_ooc("".join(inner), questions)
    return Classified(re.sub(r"\s+", " ", "".join(action)).strip(), tuple(questions))


def _take_ooc(inner: str, questions: list[str]) -> None:
    m = _KP_QUESTION.match(inner)
    if m and inner[m.end():].strip():
        questions.append(inner[m.end():].strip())


@dataclass(frozen=True)
class Received:
    """收到一条群消息之后的结果，handlers 按它回复。"""

    recorded: bool = False  # 记成了角色行动
    round_no: int = 0
    questions: tuple[str, ...] = ()
    note: str = ""  # 要回复的话（比如「好，那条算场外」）


@dataclass
class Tick:
    """一次 tick 的结果：要发到群里的话，以及刚收齐的轮次。"""

    notices: list[tuple[str, str]] = field(default_factory=list)  # (群号, 文字)
    collected: list[tuple[int, int]] = field(default_factory=list)  # (团 id, 轮次)


@dataclass(frozen=True)
class RoundState:
    campaign_id: int
    no: int
    phase: str
    started_at: int
    clock_from: int
    asked_at: int | None
    collected_at: int | None
    standby: tuple[str, ...]
    data: dict


def _round(row) -> RoundState:
    return RoundState(
        campaign_id=row["campaign_id"], no=row["no"], phase=row["phase"], started_at=row["started_at"],
        clock_from=row["clock_from"], asked_at=row["asked_at"], collected_at=row["collected_at"],
        standby=tuple(json.loads(row["standby"])), data=json.loads(row["data"]),
    )


class Rounds:
    def __init__(self, campaigns: Campaigns):
        self.campaigns = campaigns
        self.store = campaigns.store

    # ---- 读取

    def current(self, campaign: Campaign) -> RoundState | None:
        row = self.store.round(campaign.id, campaign.round)
        if row is None and campaign.phase == RUNNING and campaign.round:
            # 旧版本开的团没有轮次记录，补上
            with self.store.transaction() as db:
                self.store.insert_round(db, campaign.id, campaign.round, campaign.updated_at)
            row = self.store.round(campaign.id, campaign.round)
        return _round(row) if row else None

    def merged_actions(self, campaign_id: int, round_no: int) -> dict[str, str]:
        """QQ 号 → 这一轮的行动（同一个人的多条按顺序合并，去掉撤回和改成场外的）。"""
        merged: dict[str, list[str]] = {}
        for row in self.store.actions(campaign_id, round_no):
            if not row["withdrawn"]:
                merged.setdefault(row["user_id"], []).append(row["text"])
        return {user: "\n".join(texts) for user, texts in merged.items()}

    def waiting_for(self, campaign: Campaign) -> list[str]:
        """这一轮还没行动、也没被算作待命的在场调查员（QQ 号）。"""
        state = self.current(campaign)
        if state is None or state.phase != COLLECTING:
            return []
        acted = self.merged_actions(campaign.id, state.no)
        return [p.user_id for p in self.campaigns.players(campaign.id, (PRESENT,))
                if p.user_id not in acted and p.user_id not in state.standby]

    # ---- 收消息

    def receive(self, group_id: str, user_id: str, message_id: str, text: str, now: int) -> Received:
        campaign = self.campaigns.active(group_id)
        if campaign is None or campaign.phase != RUNNING:
            return Received()
        player = self.campaigns.player(campaign.id, user_id)
        if player is None or player.status not in (PRESENT, JOINING):
            return Received()
        parsed = classify(text)
        if not parsed.action:
            return Received(questions=parsed.questions)
        if player.status == JOINING:
            return Received(questions=parsed.questions, note=f"「{player.name}」下一轮登场，到时候再行动。")
        state = self.current(campaign)
        # 结算开始后再收到的算下一轮
        round_no = campaign.round if state is not None and state.phase == COLLECTING else campaign.round + 1
        mine = [r for r in self.store.actions(campaign.id, round_no) if r["user_id"] == user_id and not r["withdrawn"]]
        if len(mine) >= MAX_ACTIONS_PER_ROUND:
            return Received(questions=parsed.questions,
                            note=f"这一轮你已经说了 {MAX_ACTIONS_PER_ROUND} 条，后面的先不记了，等这一轮结算。")
        with self.store.transaction() as db:
            added = self.store.insert_action(db, campaign.id, round_no, user_id, message_id,
                                             parsed.action[:MAX_ACTION_CHARS], now)
            if added:
                self.store.bump_revision(db, campaign.id, now)
        return Received(recorded=added, round_no=round_no, questions=parsed.questions)

    def mark_ooc(self, group_id: str, user_id: str, replied_message_id: str, now: int) -> str:
        """回复自己的某条消息说「场外」。返回要回复的话；不相干时返回空字符串。"""
        campaign = self.campaigns.active(group_id)
        if campaign is None:
            return ""
        row = self.store.action_by_message(campaign.id, replied_message_id)
        if row is None:
            return ""
        if row["user_id"] != user_id:
            return "只能把自己说的话改成场外。"
        if row["withdrawn"]:
            return "那条已经算场外了。"
        if not self._still_open(campaign, row["round_no"]):
            return "那一轮已经开始结算了，改不了了。"
        self._withdraw(campaign, row["id"], now)
        return "好，那条算场外。"

    def recalled(self, group_id: str, message_id: str, now: int) -> bool:
        """消息被撤回：还没结算的行动就去掉。"""
        campaign = self.campaigns.active(group_id)
        if campaign is None:
            return False
        row = self.store.action_by_message(campaign.id, message_id)
        if row is None or row["withdrawn"] or not self._still_open(campaign, row["round_no"]):
            return False
        self._withdraw(campaign, row["id"], now)
        return True

    def _still_open(self, campaign: Campaign, round_no: int) -> bool:
        if round_no > campaign.round:
            return True
        state = self.store.round(campaign.id, round_no)
        return state is not None and state["phase"] == COLLECTING

    def _withdraw(self, campaign: Campaign, action_id: int, now: int) -> None:
        with self.store.transaction() as db:
            self.store.withdraw_action(db, action_id)
            self.store.bump_revision(db, campaign.id, now)

    # ---- 轮次

    def open_round(self, campaign_id: int, no: int, now: int) -> None:
        with self.store.transaction() as db:
            self.store.insert_round(db, campaign_id, no, now)

    def tick(self, now: int, timing: Timing) -> Tick:
        result = Tick()
        for campaign in self.campaigns.unfinished():
            if campaign.phase != RUNNING:
                continue
            state = self.current(campaign)
            if state is None or state.phase != COLLECTING:
                continue
            self._tick_round(campaign, state, now, timing, result)
        return result

    def _tick_round(self, campaign: Campaign, state: RoundState, now: int, timing: Timing, out: Tick) -> None:
        rows = [r for r in self.store.actions(campaign.id, state.no) if not r["withdrawn"]]
        present = self.campaigns.players(campaign.id, (PRESENT,))
        if not present:
            return
        acted = {r["user_id"] for r in rows}
        waiting = [p for p in present if p.user_id not in acted and p.user_id not in state.standby]

        if not waiting:
            if acted and now - max(r["created_at"] for r in rows) >= timing.settle_delay:
                self._collect(campaign, state, now, state.standby, out)
            return
        if not rows:
            return  # 还没人行动，不催

        start = max(min(r["created_at"] for r in rows), state.clock_from)
        names = "、".join(p.name for p in waiting)
        if state.asked_at is None:
            if now - start >= timing.ask_after:
                with self.store.transaction() as db:
                    self.store.update_round(db, campaign.id, state.no, asked_at=now)
                out.notices.append((campaign.group_id, f"还有人要行动吗？{names} 还没说要做什么。"
                                    f"{_duration(timing.standby_after)}内没有回应的话，就当原地待命。"))
            return
        if now - max(state.asked_at, state.clock_from) >= timing.standby_after:
            standby = tuple(state.standby) + tuple(p.user_id for p in waiting)
            out.notices.append((campaign.group_id, f"{names} 这一轮原地待命。"))
            self._collect(campaign, state, now, standby, out)

    def advance(self, campaign_id: int, now: int):
        """结算完：这一轮标成 done，开下一轮，处理登场和退场。返回 MembershipChange。"""
        campaign = self.campaigns.get(campaign_id)
        with self.store.transaction() as db:
            self.store.update_round(db, campaign_id, campaign.round, phase=DONE)
            self.store.update_campaign(db, campaign_id, now, round=campaign.round + 1)
            self.store.insert_round(db, campaign_id, campaign.round + 1, now)
        return self.campaigns.settle_membership(campaign_id)

    def _collect(self, campaign: Campaign, state: RoundState, now: int, standby: tuple[str, ...], out: Tick) -> None:
        with self.store.transaction() as db:
            self.store.update_round(db, campaign.id, state.no, phase=COLLECTED, collected_at=now,
                                    standby=json.dumps(list(standby)))
            self.store.bump_revision(db, campaign.id, now)
        out.collected.append((campaign.id, state.no))


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"
