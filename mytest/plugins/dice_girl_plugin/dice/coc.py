"""CoC 7 版规则：技能检定（含奖励骰、惩罚骰）、理智检定、角色卡属性解析。不依赖 NoneBot。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .expression import DiceError, roll

CRITICAL = "大成功"
EXTREME = "极难成功"
HARD = "困难成功"
SUCCESS = "成功"
FAILURE = "失败"
FUMBLE = "大失败"
SUCCESSES = (CRITICAL, EXTREME, HARD, SUCCESS)

MAX_EXTRA_DICE = 3
MAX_SKILL = 999
MAX_SKILLS = 200
MAX_NAME = 12

# 常见的别名都存成同一个名字，.st san50 之后 .ra 理智 也能找到。
ALIASES = {
    "san": "理智", "sanity": "理智", "理智值": "理智", "san值": "理智",
    "侦察": "侦查",
    "str": "力量", "con": "体质", "siz": "体型", "dex": "敏捷", "app": "外貌",
    "int": "智力", "灵感": "智力", "pow": "意志", "edu": "教育",
    "luck": "幸运", "运气": "幸运",
    "hp": "体力", "mp": "魔法", "mov": "移动",
    "cm": "克苏鲁神话", "克苏鲁": "克苏鲁神话",
}


def skill_name(name: str) -> str:
    name = name.strip()
    return ALIASES.get(name.lower(), name)


@dataclass(frozen=True)
class Check:
    roll: int
    skill: int
    level: str
    tens: tuple[int, ...] = ()  # 有奖励骰 / 惩罚骰时各个十位骰的点数
    bonus: int = 0  # 正数是奖励骰，负数是惩罚骰

    @property
    def success(self) -> bool:
        return self.level in SUCCESSES

    def header(self) -> str:
        if self.bonus:
            kind = "B" if self.bonus > 0 else "P"
            dice = "、".join(f"{t * 10:02d}" for t in self.tens)
            head = f"{kind}{abs(self.bonus)}=[十位 {dice}]{self.roll}"
        else:
            head = f"1D100={self.roll}"
        return f"{head}/{self.skill} 【{self.level}】"


def success_level(value: int, skill: int) -> str:
    if value == 1:
        return CRITICAL
    if value == 100 or (skill < 50 and value >= 96):
        return FUMBLE
    if value <= skill // 5:
        return EXTREME
    if value <= skill // 2:
        return HARD
    if value <= skill:
        return SUCCESS
    return FAILURE


def roll_d100(bonus: int = 0, rng: random.Random | None = None) -> tuple[int, tuple[int, ...]]:
    """bonus > 0 时多掷几个十位骰取最小，< 0 时取最大。00 和 0 组合是 100。"""
    rng = rng or random.Random()
    units = rng.randint(0, 9)
    tens = tuple(rng.randint(0, 9) for _ in range(1 + abs(bonus)))
    values = [t * 10 + units or 100 for t in tens]
    if bonus > 0:
        return min(values), tens
    if bonus < 0:
        return max(values), tens
    return values[0], tens


def check(skill: int, bonus: int = 0, rng: random.Random | None = None) -> Check:
    if abs(bonus) > MAX_EXTRA_DICE:
        raise DiceError(f"奖励骰、惩罚骰最多 {MAX_EXTRA_DICE} 个")
    if not 0 <= skill <= MAX_SKILL:
        raise DiceError(f"技能值要在 0 到 {MAX_SKILL} 之间")
    value, tens = roll_d100(bonus, rng)
    return Check(value, skill, success_level(value, skill), tens if bonus else (), bonus)


@dataclass(frozen=True)
class SanityCheck:
    check: Check
    loss: int
    loss_detail: str
    before: int
    after: int

    def notes(self) -> list[str]:
        notes = []
        if self.after <= 0:
            notes.append("理智归零：永久疯狂")
        elif self.loss >= 5:
            notes.append("单次损失 5 点以上：进行一次智力检定，成功则陷入临时疯狂")
        if self.before and self.loss >= self.before // 5 and self.after > 0:
            notes.append("损失达到当前理智的五分之一：守秘人可以判定为不定性疯狂")
        return notes


_SC = re.compile(r"^\s*([0-9dD+\-]+)\s*/\s*([0-9dD+\-]+)\s*(\d+)?\s*$")


def parse_sanity(text: str) -> tuple[str, str, int | None]:
    """「1/1d6 60」→ ("1", "1d6", 60)。"""
    m = _SC.match(text)
    if not m:
        raise DiceError("用法：.sc 成功损失/失败损失 [当前理智]，例：.sc 1/1d6、.sc 0/1d3 55")
    return m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None


def sanity_check(san: int, on_success: str, on_failure: str, rng: random.Random | None = None) -> SanityCheck:
    rng = rng or random.Random()
    result = check(san, 0, rng)
    if result.level == FUMBLE:
        # 大失败：失败损失取最大值
        worst = roll(on_failure, rng, require_dice=False)
        loss, detail = worst.maximum, f"{worst.expression} 取最大值={worst.maximum}"
    else:
        lost = roll(on_success if result.success else on_failure, rng, require_dice=False)
        loss, detail = lost.total, lost.detail
    loss = max(0, loss)
    return SanityCheck(result, loss, detail, san, max(0, san - loss))


# 名字后面跟数字，中间可以有 : = 或 +/-（+/- 表示在原值上加减）
_ST_ITEM = re.compile(r"([^\d\s:：=+\-,，、]+)\s*([:：=+\-]?)\s*(\d+)")


def parse_st(text: str) -> list[tuple[str, str, int]]:
    """「侦查60 图书馆:70 理智-5」→ [(侦查, "", 60), (图书馆, "", 70), (理智, "-", 5)]。"""
    items = []
    for name, op, value in _ST_ITEM.findall(text):
        name = skill_name(name)
        if len(name) > MAX_NAME:
            raise DiceError(f"「{name[:MAX_NAME]}…」这个名字太长了")
        items.append((name, op if op in "+-" else "", int(value)))
    leftover = _ST_ITEM.sub("", text).strip(" ,，、")
    if leftover or not items:
        raise DiceError("用法：.st 侦查60 图书馆70 理智50；在原值上加减写 .st 理智-5")
    return items


def apply_st(current: dict[str, int], items: list[tuple[str, str, int]]) -> dict[str, int]:
    result = dict(current)
    for name, op, value in items:
        old = result.get(name, 0)
        new = old + value if op == "+" else old - value if op == "-" else value
        result[name] = max(0, min(MAX_SKILL, new))
    if len(result) > MAX_SKILLS:
        raise DiceError(f"一张角色卡最多记 {MAX_SKILLS} 项")
    return result


_SKILL_ARG = re.compile(r"^(.*?)\s*(\d+)?\s*$", re.DOTALL)


def parse_skill_arg(text: str) -> tuple[str, int | None]:
    """「侦查 60」「侦查60」→ ("侦查", 60)；「侦查」→ ("侦查", None)；「60」→ ("", 60)。"""
    m = _SKILL_ARG.match(text.strip())
    name = skill_name(m.group(1)) if m else ""
    value = int(m.group(2)) if m and m.group(2) else None
    return name[:MAX_NAME * 2], value
