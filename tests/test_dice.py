"""骰子表达式和 CoC 规则。运行：python -m pytest tests/test_dice.py -q"""

import random

import pytest

from mytest.plugins.dice_girl_plugin.dice.coc import (
    CRITICAL,
    EXTREME,
    FAILURE,
    FUMBLE,
    HARD,
    SUCCESS,
    apply_st,
    check,
    parse_sanity,
    parse_skill_arg,
    parse_st,
    roll_d100,
    sanity_check,
    success_level,
)
from mytest.plugins.dice_girl_plugin.dice.expression import DiceError, roll


class Fixed(random.Random):
    """randint 依次返回给定的值。"""

    def __init__(self, *values: int) -> None:
        super().__init__(0)
        self.values = list(values)

    def randint(self, a: int, b: int) -> int:
        value = self.values.pop(0)
        assert a <= value <= b
        return value


# ---------------------------------------------------------------- 表达式


def test_roll_expression_detail_and_range():
    r = roll("3d6+2", Fixed(4, 2, 6))
    assert (r.total, r.detail, r.minimum, r.maximum) == (14, "3D6+2=(4+2+6)+2=14", 5, 20)
    r = roll("d20", Fixed(17))
    assert (r.expression, r.detail) == ("1D20", "1D20=17")
    r = roll("2d6+1d4-1", Fixed(3, 5, 2))
    assert (r.total, r.minimum, r.maximum) == (9, 2, 15)
    r = roll("1d6-1d4", Fixed(2, 4))
    assert (r.total, r.minimum, r.maximum) == (-2, -3, 5)


@pytest.mark.parametrize("bad", ["", "abc", "3d", "d1", "101d6", "1d1001", "5", "3d6+", "3d6++2", "1d6" + "+1" * 10])
def test_roll_rejects_bad_expressions(bad):
    with pytest.raises(DiceError):
        roll(bad)


def test_roll_allows_constants_only_when_asked():
    assert roll("1", require_dice=False).total == 1
    assert roll("0", require_dice=False).total == 0


def test_constant_detail_is_not_repeated():
    assert roll("1", require_dice=False).detail == "1"
    assert roll("1d6+0", Fixed(3)).detail == "1D6+0=3+0=3"


# ---------------------------------------------------------------- CoC 检定


@pytest.mark.parametrize("value,skill,level", [
    (1, 60, CRITICAL), (12, 60, EXTREME), (13, 60, HARD), (30, 60, HARD), (31, 60, SUCCESS),
    (60, 60, SUCCESS), (61, 60, FAILURE), (99, 60, FAILURE), (100, 60, FUMBLE),
    (96, 40, FUMBLE), (95, 40, FAILURE), (1, 0, CRITICAL),
])
def test_success_level(value, skill, level):
    assert success_level(value, skill) == level


def test_bonus_and_penalty_dice():
    # 个位 3，十位 7 和 2：奖励骰取 23，惩罚骰取 73
    assert roll_d100(1, Fixed(3, 7, 2)) == (23, (7, 2))
    assert roll_d100(-1, Fixed(3, 7, 2)) == (73, (7, 2))
    # 十位 0、个位 0 是 100
    assert roll_d100(0, Fixed(0, 0))[0] == 100
    assert roll_d100(1, Fixed(0, 0, 5))[0] == 50


def test_check_header():
    assert check(60, 0, Fixed(5, 4)).header() == "1D100=45/60 【成功】"
    assert check(60, 1, Fixed(5, 4, 1)).header() == "B1=[十位 40、10]15/60 【困难成功】"
    with pytest.raises(DiceError):
        check(60, 4)


@pytest.mark.parametrize("faces,roll_value,target,status", [
    # d100 和 .ra 一样分六档
    (100, 1, 60, CRITICAL), (100, 12, 60, EXTREME), (100, 45, 60, SUCCESS), (100, 61, 60, FAILURE),
    (100, 97, 40, FUMBLE), (100, 100, 90, FUMBLE),
    # 其他面数：最低 5%（至少 1）大成功，最高 5% 大失败，其余看是否 ≤ 目标值
    (20, 1, 15, CRITICAL), (20, 15, 15, SUCCESS), (20, 16, 15, FAILURE), (20, 20, 19, FUMBLE),
    (6, 1, 3, CRITICAL), (6, 3, 3, SUCCESS), (6, 4, 3, FAILURE), (6, 6, 5, FUMBLE),
])
def test_rd_target_check_is_lower_is_better(faces, roll_value, target, status, monkeypatch):
    from mytest.plugins.dice_girl_plugin.dice import engine

    monkeypatch.setattr(engine.random, "randint", lambda a, b: roll_value)
    result = engine.dice_logic.execute_roll(faces, target, "侦查")
    assert (result["final_roll"], result["final_status"]) == (roll_value, status)


def test_rd_without_target_has_no_verdict():
    from mytest.plugins.dice_girl_plugin.dice import dice_logic

    assert dice_logic.execute_roll(100, None, "随机数")["final_status"] == "无判定"


def test_prompt_tells_the_model_lower_is_better():
    from mytest.plugins.dice_girl_plugin.llm.prompts.dice import build_dice_user_prompt

    text = build_dice_user_prompt(50, {"event": "侦查", "target": 60, "max": 100, "final_roll": 5,
                                       "final_status": EXTREME})
    assert "越小越好" in text and "极难成功" in text


# ---------------------------------------------------------------- 理智


def test_parse_sanity():
    assert parse_sanity("1/1d6") == ("1", "1d6", None)
    assert parse_sanity(" 0/1d3 55 ") == ("0", "1d3", 55)
    with pytest.raises(DiceError):
        parse_sanity("1d6")


def test_sanity_success_failure_and_fumble():
    ok = sanity_check(60, "1", "1d6", Fixed(0, 3))  # 30，成功
    assert (ok.loss, ok.after) == (1, 59)
    bad = sanity_check(60, "1", "1d6", Fixed(0, 8, 4))  # 80，失败，扣 4
    assert (bad.loss, bad.after) == (4, 56)
    worst = sanity_check(60, "1", "1d6", Fixed(0, 0, 2))  # 100，大失败，失败损失取最大值 6
    assert (worst.loss, worst.after) == (6, 54)
    assert any("临时疯狂" in note for note in worst.notes())
    gone = sanity_check(3, "1", "1d6", Fixed(0, 8, 6))
    assert gone.after == 0 and "永久疯狂" in gone.notes()[0]


# ---------------------------------------------------------------- 角色卡


def test_parse_and_apply_st():
    items = parse_st("侦查60 图书馆:70，san50 理智-5 力量+10")
    assert items == [("侦查", "", 60), ("图书馆", "", 70), ("理智", "", 50), ("理智", "-", 5), ("力量", "+", 10)]
    sheet = apply_st({"力量": 50}, items)
    assert sheet == {"力量": 60, "侦查": 60, "图书馆": 70, "理智": 45}
    assert apply_st({}, [("理智", "-", 5)]) == {"理智": 0}
    for bad in ("", "侦查", "侦查60 乱写"):
        with pytest.raises(DiceError):
            parse_st(bad)


def test_parse_skill_arg():
    assert parse_skill_arg("侦查 60") == ("侦查", 60)
    assert parse_skill_arg("侦查60") == ("侦查", 60)
    assert parse_skill_arg("侦察") == ("侦查", None)
    assert parse_skill_arg("SAN") == ("理智", None)
    assert parse_skill_arg("60") == ("", 60)


def test_skill_sheet_storage(tmp_path, monkeypatch):
    from mytest.plugins.dice_girl_plugin.storage import repository

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "dice.db")
    monkeypatch.setattr(repository, "_initialized", False)
    repository.save_skills("1", "group:2", {"侦查": 60, "理智": 50})
    repository.save_skills("1", "private", {"侦查": 10})
    assert repository.get_skills("1", "group:2") == {"侦查": 60, "理智": 50}
    assert repository.get_skills("1", "private") == {"侦查": 10}
    repository.save_skills("1", "group:2", {})
    assert repository.get_skills("1", "group:2") == {}
