"""跑团的检定规则。运行：python -m pytest tests/test_trpg_rules.py -q"""

import random

import pytest

from mytest.plugins.dice_girl_plugin.dice.coc import CRITICAL, EXTREME, FAILURE, FUMBLE, HARD, SUCCESS
from mytest.plugins.trpg_plugin.module.model import SanityLoss
from mytest.plugins.trpg_plugin.rules.checks import (
    RuleError,
    difficulty_against,
    idea_roll,
    luck_cost,
    luck_problem,
    push,
    push_problem,
    sanity_roll,
    skill_check,
    skill_value,
    spend_luck,
    target_for,
)


class Fixed(random.Random):
    """randint 依次返回给定的值。"""

    def __init__(self, *values: int) -> None:
        super().__init__(0)
        self.values = list(values)

    def randint(self, a: int, b: int) -> int:
        value = self.values.pop(0)
        assert a <= value <= b
        return value


def d100(value: int) -> Fixed:
    """让一次不带奖励骰的 D100 掷出 value（先掷个位，再掷十位；100 = 0 和 0）。"""
    value %= 100
    return Fixed(value % 10, value // 10)


def check(value, roll, difficulty="regular", **kw):
    return skill_check("侦查", value, difficulty, rng=d100(roll), **kw)


# ---------------------------------------------------------------- 难度


def test_targets():
    assert (target_for(60, "regular"), target_for(60, "hard"), target_for(60, "extreme")) == (60, 30, 12)
    with pytest.raises(RuleError):
        target_for(60, "很难")


@pytest.mark.parametrize("value, roll, difficulty, passed, level", [
    (60, 60, "regular", True, SUCCESS),
    (60, 61, "regular", False, FAILURE),
    (60, 30, "regular", True, HARD),
    (60, 12, "regular", True, EXTREME),
    (60, 31, "hard", False, FAILURE),
    (60, 30, "hard", True, HARD),
    (60, 13, "extreme", False, FAILURE),
    (60, 12, "extreme", True, EXTREME),
    (60, 1, "extreme", True, CRITICAL),
    (60, 96, "regular", False, FAILURE),  # 技能 60：96 只是失败
    (60, 100, "regular", False, FUMBLE),
    (60, 96, "hard", False, FUMBLE),  # 困难要 30 以下，低于 50：96 起就是大失败
    (40, 96, "regular", False, FUMBLE),
    (40, 95, "regular", False, FAILURE),
    (5, 1, "extreme", True, CRITICAL),  # 01 总是大成功
    (0, 1, "regular", True, CRITICAL),
])
def test_levels(value, roll, difficulty, passed, level):
    c = check(value, roll, difficulty)
    assert (c.roll, c.passed, c.level) == (roll, passed, level)


def test_bonus_and_penalty_dice():
    c = skill_check("侦查", 60, rng=Fixed(5, 7, 2), bonus=1)  # 个位 5，十位 7 和 2，取小：25
    assert (c.roll, c.tens, c.passed) == (25, (7, 2), True)
    c = skill_check("侦查", 60, rng=Fixed(5, 7, 2), bonus=-1)  # 取大：75
    assert (c.roll, c.passed) == (75, False)
    with pytest.raises(RuleError, match="最多 2"):
        skill_check("侦查", 60, bonus=3)


def test_summary():
    assert check(60, 23, "hard").summary() == "侦查（困难） D100=23/30 困难成功，通过"
    c = skill_check("侦查", 60, rng=Fixed(5, 7, 2), bonus=1)
    assert c.summary() == "侦查（常规） 奖励骰1·D100=25/60 困难成功，通过"


def test_skill_values_from_sheet_or_base():
    sheet = {"侦查": 70, "敏捷": 60, "教育": 80}
    assert skill_value(sheet, "侦察") == 70
    assert skill_value(sheet, "图书馆") == 20
    assert skill_value(sheet, "闪避") == 30
    assert skill_value(sheet, "母语") == 80
    assert skill_check("侦察", 70, rng=d100(1)).skill == "侦查"


# ---------------------------------------------------------------- 孤注一掷


def test_push():
    first = check(60, 75)
    assert push_problem(first) == ""
    second = push(first, d100(40))
    assert second.pushed and second.passed and second.roll == 40
    assert "孤注一掷" in second.summary()
    with pytest.raises(RuleError, match="只能一次"):
        push(push(first, d100(80)), d100(10))


@pytest.mark.parametrize("skill, value, roll, reason", [
    ("侦查", 60, 30, "已经通过"),
    ("侦查", 60, 100, "大失败"),
    ("侦查", 30, 97, "大失败"),
    ("理智", 50, 70, "理智检定"),
    ("幸运", 50, 70, "幸运检定"),
    ("格斗", 50, 70, "格斗检定"),
])
def test_cannot_push(skill, value, roll, reason):
    c = skill_check(skill, value, rng=d100(roll))
    assert reason in push_problem(c)
    with pytest.raises(RuleError):
        push(c)


def test_push_keeps_bonus_dice():
    c = skill_check("侦查", 60, rng=Fixed(5, 7, 8), bonus=1)  # 75
    pushed = push(c, Fixed(3, 9, 1))  # 个位 3，十位 9 和 1，取小：13
    assert (pushed.roll, pushed.bonus, pushed.passed) == (13, 1, True)


# ---------------------------------------------------------------- 花幸运


def test_spend_luck():
    c = check(60, 68)
    assert luck_cost(c, 50) == 8
    after, cost = spend_luck(c, 50)
    assert (after.roll, after.rolled, after.luck_spent, cost, after.passed) == (60, 68, 8, 8, True)
    assert after.level == SUCCESS
    assert after.summary() == "侦查（常规） D100=68→60（花了 8 点幸运）/60 成功，通过"
    assert "已经通过" in luck_problem(after, 50)


def test_luck_on_hard_check_reaches_hard_target():
    after, cost = spend_luck(check(60, 45, "hard"), 50)
    assert (after.roll, cost, after.level) == (30, 15, HARD)


@pytest.mark.parametrize("value, roll, difficulty, luck, reason", [
    (60, 70, "regular", 5, "要花 10 点幸运，你只有 5 点"),
    (60, 100, "regular", 99, "大失败"),
    (2, 50, "extreme", 99, "技能值太低"),
])
def test_cannot_spend_luck(value, roll, difficulty, luck, reason):
    c = check(value, roll, difficulty)
    assert reason in luck_problem(c, luck) and luck_cost(c, luck) is None
    with pytest.raises(RuleError):
        spend_luck(c, luck)


def test_no_luck_after_push_or_on_sanity():
    pushed = push(check(60, 70), d100(80))
    assert "孤注一掷过" in luck_problem(pushed, 99)
    san = skill_check("理智", 50, rng=d100(60))
    assert "理智检定" in luck_problem(san, 99)


# ---------------------------------------------------------------- 对抗、灵感、理智


@pytest.mark.parametrize("opposing, difficulty", [(10, "regular"), (49, "regular"), (50, "hard"),
                                                   (89, "hard"), (90, "extreme")])
def test_difficulty_against(opposing, difficulty):
    assert difficulty_against(opposing) == difficulty


def test_idea_roll_uses_int():
    c = idea_roll({"智力": 70}, d100(65))
    assert (c.skill, c.value, c.passed) == ("智力", 70, True)
    assert idea_roll({}, d100(1)).value == 0


def test_sanity_roll_uses_module_values():
    loss = SanityLoss("0", "1d3")
    ok = sanity_roll(55, loss, Fixed(0, 3))  # 30：成功，损失 0
    assert (ok.loss, ok.after, ok.expression) == (0, 55, "0")
    bad = sanity_roll(55, loss, Fixed(0, 8, 2))  # 80：失败，1d3 掷出 2
    assert (bad.loss, bad.after, bad.expression) == (2, 53, "1d3")
    assert "理智 55→53" in bad.summary()
    worst = sanity_roll(55, loss, Fixed(0, 0, 1))  # 100：大失败，掷出 1 也按最大值 3 算
    assert (worst.loss, worst.after) == (3, 52)
    assert sanity_roll(1, SanityLoss("1", "1"), Fixed(0, 5)).after == 0
