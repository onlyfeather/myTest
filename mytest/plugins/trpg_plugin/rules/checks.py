"""CoC 7 版的检定规则：难度等级、奖励骰 / 惩罚骰、孤注一掷、花幸运、灵感检定、理智检定。不依赖 NoneBot。

掷骰用骰娘的 roll_d100 和 sanity_check，这里只加跑团需要的规则。所有骰子都由代码掷，模型只决定掷什么。

规则依据（守秘人规则书第 5、8 章）：
- 难度：常规要掷到技能值以下，困难要一半以下，极难要五分之一以下。
- 01 一定是大成功。大失败：要达到的数值低于 50 时 96–100，否则只有 100。
- 孤注一掷：检定失败后可以再掷一次，说明代价；再失败会有严重后果。理智、幸运和战斗检定不能孤注一掷，
  大失败不能孤注一掷，孤注一掷只能一次。
- 花幸运：检定之后，每花 1 点幸运把点数减 1，直到达到需要的数值。理智、幸运检定不能花，
  孤注一掷过的不能花，大失败不能花。
- 对抗：对方的技能（NPC 的 social）低于 50 是常规难度，50–89 困难，90 以上极难。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from ...dice_girl_plugin.dice.coc import (
    CRITICAL,
    EXTREME,
    FAILURE,
    FUMBLE,
    HARD,
    SUCCESS,
    SanityCheck,
    roll_d100,
    sanity_check,
)
from ..module.model import EXTREME as D_EXTREME
from ..module.model import HARD as D_HARD
from ..module.model import REGULAR, SanityLoss
from .skills import base_value, canonical

MAX_BONUS = 2
DIFFICULTY_NAMES = {REGULAR: "常规", D_HARD: "困难", D_EXTREME: "极难"}
NO_PUSH = {"理智", "幸运", "格斗", "射击", "闪避"}
NO_LUCK = {"理智", "幸运"}
IDEA = "智力"


class RuleError(ValueError):
    """规则不允许。消息可以直接给玩家看。"""


@dataclass(frozen=True)
class SkillCheck:
    skill: str
    value: int  # 技能值
    difficulty: str
    roll: int
    bonus: int = 0  # 正数奖励骰，负数惩罚骰
    tens: tuple[int, ...] = ()
    pushed: bool = False
    luck_spent: int = 0
    rolled: int = 0  # 花幸运之前掷出的点数；没花时等于 roll

    @property
    def target(self) -> int:
        return target_for(self.value, self.difficulty)

    @property
    def fumble(self) -> bool:
        return is_fumble(self.roll, self.target)

    @property
    def passed(self) -> bool:
        return self.roll == 1 or (not self.fumble and self.roll <= self.target)

    @property
    def level(self) -> str:
        """通过时是达到的成功等级（按技能值算）；没通过时是失败或大失败。"""
        if self.fumble and self.roll != 1:
            return FUMBLE
        if not self.passed:
            return FAILURE
        return level_of(self.roll, self.value, self.target)

    def summary(self) -> str:
        """主持人那段里宣布的结果，例：侦查（困难）D100=23/30 困难成功，通过。"""
        dice = "D100"
        if self.bonus:
            dice = f"{'奖励骰' if self.bonus > 0 else '惩罚骰'}{abs(self.bonus)}·D100"
        head = f"{self.skill}（{DIFFICULTY_NAMES[self.difficulty]}）"
        if self.pushed:
            head += "孤注一掷"
        roll = f"{self.rolled}→{self.roll}（花了 {self.luck_spent} 点幸运）" if self.luck_spent else str(self.roll)
        return f"{head} {dice}={roll}/{self.target} {self.level}，{'通过' if self.passed else '没有通过'}"


def target_for(value: int, difficulty: str) -> int:
    if difficulty == D_HARD:
        return value // 2
    if difficulty == D_EXTREME:
        return value // 5
    if difficulty == REGULAR:
        return value
    raise RuleError(f"难度只能是 {'、'.join(DIFFICULTY_NAMES)}")


def is_fumble(roll: int, target: int) -> bool:
    return roll == 100 or (target < 50 and roll >= 96)


def level_of(roll: int, value: int, target: int | None = None) -> str:
    if roll == 1:
        return CRITICAL
    if is_fumble(roll, value if target is None else target):
        return FUMBLE
    if roll <= value // 5:
        return EXTREME
    if roll <= value // 2:
        return HARD
    if roll <= value:
        return SUCCESS
    return FAILURE


def skill_value(sheet: dict[str, int], name: str) -> int:
    """角色卡上的值；没写就按基础值。"""
    name = canonical(name)
    return sheet[name] if name in sheet else base_value(name, sheet)


def skill_check(skill: str, value: int, difficulty: str = REGULAR, bonus: int = 0,
                rng: random.Random | None = None) -> SkillCheck:
    if abs(bonus) > MAX_BONUS:
        raise RuleError(f"奖励骰、惩罚骰最多 {MAX_BONUS} 个")
    target_for(value, difficulty)  # 检查难度
    roll, tens = roll_d100(bonus, rng)
    return SkillCheck(canonical(skill), max(0, int(value)), difficulty, roll, bonus,
                      tens if bonus else (), rolled=roll)


def push_problem(check: SkillCheck) -> str:
    """不能孤注一掷的原因；能的话返回空字符串。"""
    if check.passed:
        return "已经通过了，不用孤注一掷"
    if check.pushed:
        return "孤注一掷只能一次"
    if check.fumble:
        return "大失败不能孤注一掷"
    if check.skill in NO_PUSH:
        return f"{check.skill}检定不能孤注一掷"
    return ""


def push(check: SkillCheck, rng: random.Random | None = None) -> SkillCheck:
    """孤注一掷：同样的技能、难度、奖励骰再掷一次。"""
    problem = push_problem(check)
    if problem:
        raise RuleError(problem)
    roll, tens = roll_d100(check.bonus, rng)
    return replace(check, roll=roll, rolled=roll, tens=tens if check.bonus else (), pushed=True)


def luck_cost(check: SkillCheck, luck: int) -> int | None:
    """花多少幸运能通过；不能花或幸运不够时返回 None。"""
    return None if luck_problem(check, luck) else check.roll - check.target


def luck_problem(check: SkillCheck, luck: int) -> str:
    if check.passed:
        return "已经通过了，不用花幸运"
    if check.pushed:
        return "孤注一掷过的检定不能花幸运"
    if check.fumble:
        return "大失败不能花幸运"
    if check.skill in NO_LUCK:
        return f"{check.skill}检定不能花幸运"
    if check.luck_spent:
        return "这次检定已经花过幸运了"
    if check.target < 1:
        return "这个难度下技能值太低，花幸运也达不到"
    cost = check.roll - check.target
    if cost > luck:
        return f"要花 {cost} 点幸运，你只有 {luck} 点"
    return ""


def spend_luck(check: SkillCheck, luck: int) -> tuple[SkillCheck, int]:
    """返回（花完之后的检定，花了几点）。"""
    problem = luck_problem(check, luck)
    if problem:
        raise RuleError(problem)
    cost = check.roll - check.target
    return replace(check, roll=check.target, luck_spent=cost), cost


def difficulty_against(opposing: int) -> str:
    """对 NPC 做社交检定时，按 NPC 的数值定难度。"""
    if opposing >= 90:
        return D_EXTREME
    if opposing >= 50:
        return D_HARD
    return REGULAR


def idea_roll(sheet: dict[str, int], rng: random.Random | None = None) -> SkillCheck:
    """灵感检定：掷智力，常规难度。"""
    return skill_check(IDEA, skill_value(sheet, IDEA), REGULAR, 0, rng)


@dataclass(frozen=True)
class SanityResult:
    check: SanityCheck
    expression: str  # 用到的损失表达式

    @property
    def loss(self) -> int:
        return self.check.loss

    @property
    def after(self) -> int:
        return self.check.after

    def summary(self) -> str:
        c = self.check
        ok = "成功" if c.check.success else c.check.level
        return f"理智检定 D100={c.check.roll}/{c.before} {ok}，损失 {c.loss_detail}，理智 {c.before}→{c.after}"


def sanity_roll(san: int, loss: SanityLoss, rng: random.Random | None = None) -> SanityResult:
    """理智检定：损失数值来自模组。"""
    result = sanity_check(max(0, san), loss.success, loss.fail, rng)
    return SanityResult(result, loss.success if result.check.success else loss.fail)
