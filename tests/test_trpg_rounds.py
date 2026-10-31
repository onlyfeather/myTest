"""跑团每一轮的行动收集、场外、超时。运行：python -m pytest tests/test_trpg_rounds.py -q"""

import pytest

from mytest.plugins.trpg_plugin.campaign import LEFT, PRESENT, Actor, Campaigns, ModuleLibrary
from mytest.plugins.trpg_plugin.rounds import COLLECTED, COLLECTING, DONE, Rounds, Timing, classify
from mytest.plugins.trpg_plugin.store import Store

GROUP = "1130489499"
LIN, ZHAO, SU = "1001", "1002", "1003"
T = Timing(ask_after=30 * 60, standby_after=12 * 3600, settle_delay=60)
T0 = 1_700_000_000


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "trpg.db")
    clock = Clock()
    campaigns = Campaigns(store, ModuleLibrary(store), clock)
    campaigns.open(GROUP, "茶馆失物", Actor(LIN))
    campaigns.join(GROUP, Actor(LIN), pregen="林晓")
    campaigns.join(GROUP, Actor(ZHAO), pregen="赵川")
    campaigns.start(GROUP, Actor(LIN))
    yield campaigns, Rounds(campaigns), clock
    store.close()


def say(env, user, text, message_id, at=None):
    campaigns, rounds, clock = env
    return rounds.receive(GROUP, user, str(message_id), text, clock.now if at is None else at)


def state(env):
    campaigns, rounds, _ = env
    return rounds.current(campaigns.active(GROUP))


# ---------------------------------------------------------------- 场外判断


@pytest.mark.parametrize("text, action, questions", [
    ("我推开门", "我推开门", ()),
    ("（我去倒杯水）", "", ()),
    ("(brb)", "", ()),
    ("我看看柜台（手机没电了）", "我看看柜台", ()),
    ("（KP 侦查能用来看脚印吗）", "", ("侦查能用来看脚印吗",)),
    ("（kp：我还有几点理智）我翻登记簿", "我翻登记簿", ("我还有几点理智",)),
    ("我问老周（KP 他看起来紧张吗）然后看他的鞋", "我问老周然后看他的鞋", ("他看起来紧张吗",)),
    ("（嵌套（里面）还是场外）", "", ()),
    ("我走过去（忘了写右括号", "我走过去", ()),
    ("KP 我能看到什么", "KP 我能看到什么", ()),  # 没加括号就是角色行动，可以回复「场外」改掉
    ("（KP）", "", ()),
    (".ra 侦查", "", ()),
    ("/kp 状态", "", ()),
    ("   ", "", ()),
])
def test_classify(text, action, questions):
    result = classify(text)
    assert (result.action, result.questions) == (action, questions)


# ---------------------------------------------------------------- 收集


def test_start_opens_round_one(env):
    s = state(env)
    assert (s.no, s.phase, s.started_at) == (1, COLLECTING, T0)


def test_actions_are_recorded_and_merged(env):
    campaigns, rounds, _ = env
    assert say(env, LIN, "我看柜台", 1).recorded
    assert say(env, LIN, "（喝口水）", 2).recorded is False
    assert say(env, LIN, "顺便翻登记簿", 3).recorded
    assert say(env, ZHAO, "我去看后门的锁", 4).recorded
    c = campaigns.active(GROUP)
    assert rounds.merged_actions(c.id, 1) == {LIN: "我看柜台\n顺便翻登记簿", ZHAO: "我去看后门的锁"}


def test_same_message_only_once(env):
    assert say(env, LIN, "我看柜台", 1).recorded
    assert not say(env, LIN, "我看柜台", 1).recorded


def test_non_players_and_paused_are_ignored(env):
    campaigns, rounds, _ = env
    assert not say(env, "5555", "我也来", 1).recorded
    campaigns.pause(GROUP, Actor(LIN))
    assert not say(env, LIN, "我看柜台", 2).recorded


def test_joining_player_is_told_to_wait(env):
    campaigns, _, _ = env
    campaigns.join(GROUP, Actor(SU), pregen="苏禾")
    result = say(env, SU, "我看看壶的木托", 1)
    assert not result.recorded and "下一轮登场" in result.note


def test_questions_are_returned(env):
    result = say(env, LIN, "我看柜台（KP 柜台多高）", 1)
    assert result.recorded and result.questions == ("柜台多高",)


def test_too_many_actions(env):
    for i in range(10):
        assert say(env, LIN, f"第{i}条", i).recorded
    result = say(env, LIN, "第11条", 99)
    assert not result.recorded and "10 条" in result.note


def test_revision_changes_on_action(env):
    campaigns, _, _ = env
    before = campaigns.active(GROUP).revision
    say(env, LIN, "我看柜台", 1)
    assert campaigns.active(GROUP).revision == before + 1


# ---------------------------------------------------------------- 改成场外、撤回


def test_reply_ooc_withdraws_own_action(env):
    campaigns, rounds, clock = env
    say(env, LIN, "KP 我能看到什么", 1)
    assert rounds.mark_ooc(GROUP, ZHAO, "1", clock.now) == "只能把自己说的话改成场外。"
    assert rounds.mark_ooc(GROUP, LIN, "1", clock.now) == "好，那条算场外。"
    assert rounds.mark_ooc(GROUP, LIN, "1", clock.now) == "那条已经算场外了。"
    assert rounds.mark_ooc(GROUP, LIN, "404", clock.now) == "", "回复的不是行动，不理"
    assert rounds.merged_actions(campaigns.active(GROUP).id, 1) == {}


def test_recall_withdraws_action(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    assert rounds.recalled(GROUP, "1", clock.now)
    assert not rounds.recalled(GROUP, "1", clock.now)
    assert rounds.merged_actions(campaigns.active(GROUP).id, 1) == {}
    assert rounds.waiting_for(campaigns.active(GROUP)) == [LIN, ZHAO]


def test_cannot_withdraw_after_settling_started(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    say(env, ZHAO, "我看锁", 2)
    clock.now += 60
    assert rounds.tick(clock.now, T).collected
    assert rounds.mark_ooc(GROUP, LIN, "1", clock.now) == "那一轮已经开始结算了，改不了了。"
    assert not rounds.recalled(GROUP, "2", clock.now)


def test_actions_after_collection_go_to_next_round(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    say(env, ZHAO, "我看锁", 2)
    clock.now += 60
    rounds.tick(clock.now, T)
    result = say(env, LIN, "我再看一眼地面", 3)
    assert result.recorded and result.round_no == 2
    assert rounds.mark_ooc(GROUP, LIN, "3", clock.now) == "好，那条算场外。", "下一轮还没开始结算，可以改"
    c = campaigns.active(GROUP)
    assert rounds.merged_actions(c.id, 1) == {LIN: "我看柜台", ZHAO: "我看锁"}


# ---------------------------------------------------------------- 计时


def test_collects_when_everyone_acted_and_quiet(env):
    _, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    say(env, ZHAO, "我看锁", 2)
    clock.now += 59
    assert not rounds.tick(clock.now, T).collected, "刚说完，等他补充"
    clock.now += 1
    result = rounds.tick(clock.now, T)
    assert result.collected and not result.notices
    assert state(env).phase == COLLECTED and state(env).standby == ()
    assert not rounds.tick(clock.now + 999, T).collected, "只收齐一次"


def test_no_nagging_before_anyone_acts(env):
    _, rounds, clock = env
    assert rounds.tick(clock.now + 10 * 24 * 3600, T).notices == []


def test_ask_then_standby(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    clock.now += 30 * 60 - 1
    assert not rounds.tick(clock.now, T).notices
    clock.now += 1
    result = rounds.tick(clock.now, T)
    assert result.notices == [(GROUP, "还有人要行动吗？赵川 还没说要做什么。12 小时内没有回应的话，就当原地待命。")]
    assert not rounds.tick(clock.now + 60, T).notices, "只问一次"
    assert rounds.waiting_for(campaigns.active(GROUP)) == [ZHAO]

    clock.now += 12 * 3600
    result = rounds.tick(clock.now, T)
    assert result.notices == [(GROUP, "赵川 这一轮原地待命。")]
    assert result.collected and state(env).standby == (ZHAO,)


def test_late_reply_after_asking_collects_normally(env):
    _, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    clock.now += 30 * 60
    rounds.tick(clock.now, T)
    clock.now += 3600
    say(env, ZHAO, "我看锁", 2)
    clock.now += 60
    result = rounds.tick(clock.now, T)
    assert result.collected and not result.notices and state(env).standby == ()


def test_pause_resets_clock(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    clock.now += 30 * 60
    rounds.tick(clock.now, T)
    campaigns.pause(GROUP, Actor(LIN))
    clock.now += 24 * 3600
    assert not rounds.tick(clock.now, T).collected, "暂停中不计时"
    campaigns.resume(GROUP, Actor(LIN))
    assert state(env).asked_at is None
    clock.now += 30 * 60
    assert rounds.tick(clock.now, T).notices, "继续后重新问"
    clock.now += 12 * 3600
    assert rounds.tick(clock.now, T).collected


def test_leaving_player_is_not_waited_for(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    campaigns.leave(GROUP, Actor(ZHAO))
    clock.now += 60
    assert rounds.tick(clock.now, T).collected


# ---------------------------------------------------------------- 进入下一轮


def test_advance_opens_next_round_and_settles_membership(env):
    campaigns, rounds, clock = env
    campaigns.join(GROUP, Actor(SU), pregen="苏禾")
    say(env, LIN, "我看柜台", 1)
    say(env, LIN, "再翻翻登记簿", 2)
    campaigns.leave(GROUP, Actor(ZHAO))
    clock.now += 60
    (cid, no), = rounds.tick(clock.now, T).collected
    assert rounds.merged_actions(cid, no) == {LIN: "我看柜台\n再翻翻登记簿"}

    change = rounds.advance(cid, clock.now)
    assert [p.name for p in change.entered] == ["苏禾"] and [p.name for p in change.exited] == ["赵川"]
    c = campaigns.active(GROUP)
    assert c.round == 2 and state(env).phase == COLLECTING
    assert campaigns.store.round(cid, 1)["phase"] == DONE
    assert campaigns.player(cid, SU).status == PRESENT and campaigns.player(cid, ZHAO).status == LEFT
    assert rounds.waiting_for(c) == [LIN, SU]


def test_standby_recorded_on_round(env):
    campaigns, rounds, clock = env
    say(env, LIN, "我看柜台", 1)
    clock.now += 30 * 60
    rounds.tick(clock.now, T)
    clock.now += 12 * 3600
    rounds.tick(clock.now, T)
    assert state(env).standby == (ZHAO,)


def test_campaign_without_round_row_gets_one(env):
    campaigns, rounds, _ = env
    c = campaigns.active(GROUP)
    with campaigns.store.transaction() as db:
        db.execute("DELETE FROM round WHERE campaign_id = ?", (c.id,))
    assert say(env, LIN, "我看柜台", 1).round_no == 1
    assert state(env).phase == COLLECTING


def test_survives_restart(tmp_path):
    path = tmp_path / "trpg.db"
    clock = Clock()
    s1 = Store(path)
    c1 = Campaigns(s1, ModuleLibrary(s1), clock)
    c1.open(GROUP, "茶馆失物", Actor(LIN))
    c1.join(GROUP, Actor(LIN), pregen="林晓")
    c1.join(GROUP, Actor(ZHAO), pregen="赵川")
    c1.start(GROUP, Actor(LIN))
    Rounds(c1).receive(GROUP, LIN, "1", "我看柜台", clock.now)
    s1.close()

    s2 = Store(path)
    rounds = Rounds(Campaigns(s2, ModuleLibrary(s2), clock))
    clock.now += 30 * 60
    assert rounds.tick(clock.now, T).notices, "重启后接着计时"
    s2.close()
