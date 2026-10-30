"""跑团群里的指令。运行：python -m pytest tests/test_trpg_commands.py -q"""

import pytest

from mytest.plugins.trpg_plugin.campaign import Actor, Campaigns, ModuleLibrary
from mytest.plugins.trpg_plugin.commands import Caller, run
from mytest.plugins.trpg_plugin.store import Store

GROUP = "1130489499"


@pytest.fixture
def campaigns(tmp_path):
    store = Store(tmp_path / "trpg.db")
    yield Campaigns(store, ModuleLibrary(store))
    store.close()


def caller(user="1001", *, name="千树", manager=False, sheet=None):
    return Caller(GROUP, Actor(user, manager), name, lambda: dict(sheet or {}))


def say(campaigns, command, arg="", who=None):
    return run(campaigns, command, arg, who or caller())


def test_full_lifecycle(campaigns):
    text = say(campaigns, "开团", "茶馆失物")
    assert "开始招募" in text and "1–3 人" in text and "林晓、赵川、苏禾" in text
    assert "加入了" in say(campaigns, "加入", "林晓")
    alice = caller("1002", name="阿丽丝", sheet={"侦查": 60, "理智": 50, "智力": 70, "幸运": 40})
    assert "「阿丽丝」加入了。现在 2 人" in say(campaigns, "加入", "", alice)
    assert "开团的人" in say(campaigns, "开始", "", alice)

    text = say(campaigns, "开始")
    assert "林晓、阿丽丝" in text and "雨夜" in text and "场外" in text
    assert "第 1 轮，在茶馆大堂" in say(campaigns, "kp", "状态")

    bob = caller("1003", name="鲍勃")
    assert "下一轮登场" in say(campaigns, "加入", "苏禾", bob)
    assert "下一轮离开" in say(campaigns, "离开", "", alice)

    assert "暂停" in say(campaigns, "暂停")
    assert say(campaigns, "继续") == "继续。"
    assert "结束了" in say(campaigns, "结束")
    assert "没有团" in say(campaigns, "kp")


def test_join_without_sheet_points_to_st_and_pregens(campaigns):
    say(campaigns, "开团", "茶馆失物")
    text = say(campaigns, "加入")
    assert ".st" in text and "/加入 林晓" in text


def test_errors_are_replies(campaigns):
    assert "没有团" in say(campaigns, "加入", "林晓")
    assert "没有叫" in say(campaigns, "开团", "不存在")
    say(campaigns, "开团", "茶馆失物")
    assert "已经有一个" in say(campaigns, "开团", "茶馆失物")
    assert "可用" in say(campaigns, "kp", "乱写")


def test_last_player_leaving_pauses(campaigns):
    say(campaigns, "开团", "茶馆失物")
    say(campaigns, "加入", "林晓")
    say(campaigns, "开始")
    assert "先暂停" in say(campaigns, "离开")


def test_manager_can_end_someone_elses_campaign(campaigns):
    say(campaigns, "开团", "茶馆失物")
    assert "开团的人" in say(campaigns, "结束", "", caller("1005"))
    assert "结束了" in say(campaigns, "结束", "", caller("1005", manager=True))
