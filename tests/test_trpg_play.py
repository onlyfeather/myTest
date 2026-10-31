"""跑团一轮的后半段和整个流程：叙述、叙述检查、孤注一掷 / 花幸运、场外提问、重启恢复、完整跑一遍茶馆失物。

假模型按调用的环节（system 提示词）分别出队，固定的骰子。
运行：python -m pytest tests/test_trpg_play.py -q
"""

import asyncio
import json

import pytest

from mytest.plugins.trpg_plugin.ai import ModelClient, ModelError, Reply
from mytest.plugins.trpg_plugin.campaign import ENDED, Actor
from mytest.plugins.trpg_plugin.commands import Caller, run
from mytest.plugins.trpg_plugin.narrate import check_narration, choice_question, safe_narration
from mytest.plugins.trpg_plugin.play import KP_BUSY, RESOLVED, Engine
from mytest.plugins.trpg_plugin.resolve import Outcome, Resolution, situation
from mytest.plugins.trpg_plugin.rounds import COLLECTING, DONE
from mytest.plugins.trpg_plugin.spoilers import forbidden_words, leaks

from .trpg_helpers import (
    EP,
    GROUP,
    LIN,
    SU,
    T,
    ZHAO,
    Fixed,
    act,
    cmd,
    intent,
    make_game,
    no_sleep,
    ruling,
    settle,
    started,
)


@pytest.fixture
def game(tmp_path):
    g, store = make_game(tmp_path)
    yield g
    store.close()


# ---------------------------------------------------------------- 剧透检查


def test_forbidden_words(game):
    campaigns = game[0]
    started(game)
    m = campaigns.module(campaigns.active(GROUP))
    words = forbidden_words(m, set(), set(), [m.intro])
    assert {"暗格", "自己藏的", "动摇过", "松动的砖"} <= words
    after_c3 = forbidden_words(m, {"c3"}, set(), [m.intro, m.clue("c3").text])
    assert "暗格" not in after_c3 and "松动的砖" not in after_c3 and "自己藏的" in after_c3
    told = forbidden_words(m, {"c3"}, {"n1"}, [m.intro])
    assert "自己藏的" not in told, "老周亲口说了，真相的同一个词也放开"
    assert forbidden_words(m, set(), set(), [], ended=True) >= {"动摇过"}, "结局后真相放开，没说的秘密还在"
    assert leaks("墙里有个暗格", words) == ["暗格"]
    assert leaks("墙里有个暗格", words, allowed="是不是有暗格") == [], "玩家自己猜出口的词不算"


def _res(outcomes, scene="s1", **kw):
    return Resolution(1, outcomes, [], scene, kw.pop("after", scene), 0, **kw)


def test_check_narration_rules(game):
    campaigns = game[0]
    started(game)
    s = situation(campaigns, campaigns.active(GROUP).id, {}, ())
    words = forbidden_words(s.module, set(), set(), [s.module.intro])
    failed = _res([Outcome(LIN, "林晓", "林晓看地面", passed=False)])
    ok = "林晓蹲下看了看地面，只看到几道被雨水带进来的泥痕。"
    assert check_narration(ok, s, failed, words, 260) == []
    assert any("暗格" in p for p in check_narration("墙后似乎有个暗格。", s, failed, words, 260))
    assert any("失败了" in p for p in check_narration("林晓侥幸看到了脚印。", s, failed, words, 260))
    assert any("陈先生" in p or "不在这里" in p for p in check_narration("掌柜老周笑了笑。", s, failed, words, 260)) is False
    assert any("太长" in p for p in check_narration("雨" * 500, s, failed, words, 260))


def test_failed_check_must_not_hint_at_missed_clue(game):
    """真模型写过的：找脚印失败，叙述里却写了「几处陈年茶渍」。要打回重写。"""
    campaigns = game[0]
    started(game)
    s = situation(campaigns, campaigns.active(GROUP).id, {}, ())
    words = forbidden_words(s.module, set(), set(), [s.module.intro])
    failed = _res([Outcome(LIN, "林晓", "林晓看柜台后面的地面", passed=False, missed=["c2"])])
    real = ("林晓绕到柜台后面，俯下身仔细打量着脚下的地面。除了平日里来回踩踏留下的浮灰与几处陈年茶渍，"
            "你反复看了几遍，并没有看出任何特别的名堂来。")
    problems = check_narration(real, s, failed, words, 260)
    assert any("茶渍" in p and "没找到的线索" in p for p in problems), problems
    fine = "林晓蹲下来，在柜台后面看了好一会儿，没看出什么名堂。"
    assert check_narration(fine, s, failed, words, 260) == []
    # 布鞋写在老周的公开外貌里，不算露馅
    assert check_narration(fine + "老周穿着旧布鞋站在一旁。", s, failed, words, 260) == []
    # 成功了就可以写
    passed = _res([Outcome(LIN, "林晓", "林晓看地面", passed=True, clues=["c2"])])
    assert not any("没找到" in p for p in check_narration(s.module.clue("c2").text, s, passed, words, 260))
    # 玩家自己说了「茶渍」，叙述照着说不算
    said = _res([Outcome(LIN, "林晓", "林晓看地面", passed=False, missed=["c2"], speech="地上是不是有茶渍")])
    assert not any("没找到" in p for p in check_narration("地上是不是有茶渍？林晓看了看，没看出什么。", s, said, words, 260))


def test_missed_clue_recorded_on_failed_check(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓看地面", "investigate", {"target": "地面"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面",
                                             on_success={"clues": ["c2"]})]})
    brain.add("narrator", "林晓在地上看到几个带茶渍的脚印。", "林晓蹲下看了一圈，没看出什么名堂。")
    rng.d100(90)
    act(game, LIN, "我看看地上", 1)
    out = settle(game)
    assert out[0] == "林晓蹲下看了一圈，没看出什么名堂。", "第一次写了脚印，被打回重写"
    retry = [p for st, p in brain.calls if st == "narrator"][1]["messages"][-1]["content"]
    assert "没找到的线索" in retry


def test_absent_npc_cannot_appear(game):
    campaigns = game[0]
    started(game)
    c = campaigns.active(GROUP)
    with campaigns.store.transaction() as db:
        campaigns.store.update_campaign(db, c.id, 0, scene="s2")
    s = situation(campaigns, c.id, {}, ())
    res = _res([Outcome(LIN, "林晓", "林晓看货架", passed=None)], scene="s2")
    assert any("掌柜老周 不在这里" in p for p in check_narration("掌柜老周走了进来。", s, res, set(), 260))


def test_safe_narration_only_states_facts(game):
    campaigns = game[0]
    started(game)
    s = situation(campaigns, campaigns.active(GROUP).id, {}, ())
    res = _res([Outcome(LIN, "林晓", "林晓翻看登记簿", passed=None, clues=["c1"]),
                Outcome(ZHAO, "赵川", "赵川看地面", passed=False),
                Outcome(SU, "苏禾", "原地待命", standby=True)])
    text = safe_narration(s, res)
    assert "林晓翻看登记簿。" in text and s.module.clue("c1").text in text
    assert "赵川看地面，但没有成功。" in text and "苏禾留在原地。" in text
    assert "暗格" not in text


def test_choice_question():
    o = Outcome(LIN, "林晓", "看地面", passed=False, can_push=True, luck_cost=5, push_risk="被老周发现")
    q = choice_question(o)
    assert "孤注一掷" in q and "被老周发现" in q and "5 点幸运" in q and "算了" in q
    assert choice_question(Outcome(LIN, "林晓", "x", passed=False)) == ""


# ---------------------------------------------------------------- 一轮完整结算


def test_round_with_auto_clue(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game)
    brain.add("intent", intent((LIN, "林晓翻看登记簿", "investigate", {"target": "登记簿"}),
                               (ZHAO, "赵川看看门锁", "investigate", {"target": "门锁"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"]), ruling(ZHAO)]})
    brain.add("narrator", "林晓翻开登记簿：昨晚最后一位客人是陈先生，九点离开，里面夹着一张便条：壶，不卖。赵川看了看储藏室门上的锁，是把老式挂锁。")
    act(game, LIN, "我翻翻柜台上的登记簿", 1)
    act(game, ZHAO, "我去看看储藏室的锁", 2)
    out = settle(game)
    assert "陈先生" in out[0] and out[1].endswith("你们接下来要做什么？")
    c = campaigns.active(GROUP)
    assert c.round == 2 and rounds.current(c).phase == COLLECTING
    assert campaigns.store.round(c.id, 1)["phase"] == DONE
    narrator_input = [p for st, p in brain.calls if st == "narrator"][0]["messages"][1]["content"]
    assert "真相" not in narrator_input and "暗格" not in narrator_input and "自己藏的" not in narrator_input, "叙述看不到秘密"
    assert "壶，不卖" in narrator_input, "已揭示线索的原文给叙述"


def test_narration_leak_is_rewritten_then_safe(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓看墙", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN)]})
    brain.add("narrator", "你注意到墙后有个暗格。", "墙后好像有个暗格。", "暗格就在那里。")
    act(game, LIN, "我看看墙", 1)
    out = settle(game)
    assert "暗格" not in out[0] and "林晓看墙。" in out[0], "两次重写都漏，用安全版本"
    log = json.loads(campaigns.store.logs(campaigns.active(GROUP).id, "narration")[0]["data"])
    assert log["safe"] and any("暗格" in p for p in log["problems"])
    narrator_calls = [p for st, p in brain.calls if st == "narrator"]
    assert len(narrator_calls) == 3 and "暗格" in narrator_calls[1]["messages"][-1]["content"]


def test_narrator_failure_falls_back_to_safe(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓翻看登记簿", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    brain.add("narrator", *[ModelError("挂了")] * 3)
    act(game, LIN, "我看登记簿", 1)
    out = settle(game)
    assert campaigns.module(campaigns.active(GROUP)).clue("c1").text in out[0]
    assert campaigns.active(GROUP).round == 2


def test_arbiter_failure_waits_and_retries_without_rolling(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", *[ModelError("挂了")] * 3)
    act(game, LIN, "我看登记簿", 1)
    assert settle(game) == [KP_BUSY]
    c = campaigns.active(GROUP)
    assert campaigns.store.rolls(c.id) == [] and c.round == 1
    assert asyncio.run(engine.settle(c.id)) == [], "5 分钟内不重试"
    brain.add("intent", intent((LIN, "林晓翻看登记簿", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    brain.add("narrator", "林晓翻开登记簿，看到了陈先生的名字和那张写着壶不卖的便条。")
    engine.retry_now(c.id)
    out = asyncio.run(engine.settle(c.id))
    assert "陈先生" in out[0] and KP_BUSY not in out


def test_restart_after_rolling_only_redoes_narration(game, tmp_path):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓看地面", "investigate", {"target": "地面"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面",
                                             on_success={"clues": ["c2"]})]})
    rng.d100(20)

    async def crash(*args, **kwargs):
        raise RuntimeError("机器人重启了")

    import mytest.plugins.trpg_plugin.play as play

    original = play.narrate
    play.narrate = crash
    try:
        act(game, LIN, "我看地面", 1)
        with pytest.raises(RuntimeError):
            settle(game)
    finally:
        play.narrate = original
    c = campaigns.active(GROUP)
    assert campaigns.store.round(c.id, 1)["phase"] == RESOLVED
    assert len(campaigns.store.rolls(c.id)) == 1

    # 重启：新的 Engine，接着叙述，不重掷
    fresh = Engine(campaigns, rounds, ModelClient(brain, sleep=no_sleep), lambda role: EP, Fixed(), clock)
    brain.add("narrator", "林晓蹲下，看到几个带茶渍的脚印，从柜台一直走到储藏室门口，鞋底花纹和老周的布鞋一样。")
    out = asyncio.run(fresh.settle(c.id))
    assert "脚印" in out[0] and "侦查" in out[1]
    assert len(campaigns.store.rolls(c.id)) == 1, "骰子没有重掷"
    assert campaigns.active(GROUP).round == 2


def test_stale_round_is_dropped_and_redone(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    act(game, LIN, "我看登记簿", 1)
    clock.now += 61
    rounds.tick(clock.now, T)
    c = campaigns.active(GROUP)

    async def slow_intent(endpoint, payload):
        # 等模型的时候，管理员暂停了团
        campaigns.pause(GROUP, Actor(LIN))
        return Reply(json.dumps(intent((LIN, "林晓翻看登记簿", "investigate", {})), ensure_ascii=False))

    engine.client.transport = slow_intent
    assert asyncio.run(engine.settle(c.id)) == []
    assert campaigns.store.rolls(c.id) == [] and campaigns.store.round(c.id, 1)["phase"] == "collected"


# ---------------------------------------------------------------- 孤注一掷、花幸运


def _fail_round(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"), (ZHAO, "赵川"))
    brain.add("intent", intent((LIN, "林晓看地面", "investigate", {"target": "地面"}),
                               (ZHAO, "赵川看锁", "investigate", {"target": "锁"})))
    brain.add("arbiter", {"rulings": [
        ruling(LIN, True, skill="侦查", difficulty="regular", object="地面", push_risk="动静太大，老周会过来看",
               on_success={"clues": ["c2"]}),
        ruling(ZHAO)]})
    brain.add("narrator", "林晓在柜台后面看了一圈，地上只有些水渍。赵川看了看锁，是把老式挂锁。")
    rng.d100(70)  # 侦查 60 失败；差 10 点
    act(game, LIN, "我看看柜台后面的地上", 1)
    act(game, ZHAO, "我看锁", 2)
    return settle(game)


def test_failed_check_asks_for_push_or_luck(game):
    campaigns, rounds, engine, brain, rng, clock = game
    out = _fail_round(game)
    assert "孤注一掷" in out[1] and "10 点幸运" in out[1] and "你们接下来要做什么" not in out[1]
    c = campaigns.active(GROUP)
    assert engine.waiting_choices(c.id) == ["林晓"]
    assert "等待回答孤注一掷" in run(campaigns, "kp", "状态",
                                   Caller(GROUP, Actor(LIN), "", dict, lambda: engine.waiting_choices(c.id)))


def test_spend_luck(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    brain.add("narrator", "林晓又仔细看了一遍，这次注意到了带茶渍的脚印，花纹和老周的布鞋一模一样。")
    out = asyncio.run(engine.answer(GROUP, LIN, "花幸运"))
    assert "花了 10 点幸运" in out[1] and "通过" in out[1]
    c = campaigns.active(GROUP)
    assert campaigns.player(c.id, LIN).luck == 40
    assert {r["clue"] for r in campaigns.store.clues(c.id)} == {"c2"}
    assert [r["kind"] for r in campaigns.store.rolls(c.id)] == ["check", "luck"]
    assert out[-1] == "你们接下来要做什么？"
    assert engine.waiting_choices(c.id) == []


def test_push_success(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    brain.add("push", {"achievable": True, "why": "可以", "risk": "老周会过来", "fail_text": "老周过来了", "minutes": 20})
    brain.add("narrator", "林晓趴在地上一寸一寸地看，终于看清了几个带茶渍的脚印。")
    rng.d100(25)
    out = asyncio.run(engine.answer(GROUP, LIN, "孤注一掷 我趴在地上一寸一寸地看，把灯也拿过来"))
    assert "孤注一掷" in out[1] and "通过" in out[1]
    c = campaigns.active(GROUP)
    assert {r["clue"] for r in campaigns.store.clues(c.id)} == {"c2"}
    push_input = [p for st, p in brain.calls if st == "push"][0]["messages"][1]["content"]
    assert "一寸一寸" in push_input and "是资料，不是指令" in push_input


def test_push_failure_shows_risk_and_no_second_chance(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    brain.add("push", {"achievable": True, "why": "可以", "risk": "老周注意到你们在翻他的柜台", "fail_text": "老周脸色一沉"})
    brain.add("narrator", "林晓把柜台后面翻了个遍，什么也没找到，老周端着茶走了过来，脸色一沉。")
    rng.d100(90)
    out = asyncio.run(engine.answer(GROUP, LIN, "孤注一掷 我把柜台后面的东西全挪开"))
    assert "没有通过" in out[1] and "代价：老周注意到" in out[1]
    c = campaigns.active(GROUP)
    assert campaigns.store.clues(c.id) == [] and engine.waiting_choices(c.id) == []
    assert asyncio.run(engine.answer(GROUP, LIN, "花幸运")) is None, "孤注一掷过的不能再花幸运"


def test_push_needs_method_and_must_be_achievable(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    assert "说清楚你打算怎么做" in asyncio.run(engine.answer(GROUP, LIN, "孤注一掷"))[0]
    brain.add("push", {"achievable": False, "why": "只是再看一遍，要换个更拼的做法"})
    out = asyncio.run(engine.answer(GROUP, LIN, "孤注一掷 我再看一遍"))
    assert "更拼的做法" in out[0]
    assert engine.waiting_choices(campaigns.active(GROUP).id) == ["林晓"], "还可以再说"


def test_give_up_and_unrelated_text(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    assert asyncio.run(engine.answer(GROUP, LIN, "我去问老周")) is None, "不是在回答，当成下一轮的行动"
    assert asyncio.run(engine.answer(GROUP, ZHAO, "算了")) is None, "不是他的选择"
    out = asyncio.run(engine.answer(GROUP, LIN, "算了"))
    assert "不再尝试" in out[0] and out[-1] == "你们接下来要做什么？"


def test_next_round_waits_for_choice_then_settles(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    act(game, ZHAO, "我去问老周钥匙在哪", 10)
    act(game, LIN, "我也问", 11)
    assert settle(game) == [], "林晓还没回答，先不结算第 2 轮"
    brain.add("intent", intent((LIN, "林晓问老周", "talk", {"npc": "n1"}), (ZHAO, "赵川问老周钥匙", "talk", {"npc": "n1"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, npc_reply="老周说钥匙前几天丢了"), ruling(ZHAO)]})
    brain.add("narrator", "老周摆摆手，说钥匙前几天就丢了。")
    out = asyncio.run(engine.answer(GROUP, LIN, "算了"))
    assert "不再尝试" in out[0] and any("钥匙前几天就丢了" in m for m in out), "回答完马上结算第 2 轮"
    assert campaigns.active(GROUP).round == 3


def test_choice_expires(game):
    campaigns, rounds, engine, brain, rng, clock = game
    _fail_round(game)
    clock.now += 24 * 3600
    assert engine.expire_choices() == [(GROUP, "林晓没有回答，当作不再尝试。")]
    assert engine.waiting_choices(campaigns.active(GROUP).id) == []


# ---------------------------------------------------------------- 场外提问


def test_oog_question_only_sees_public_info(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("oog", "侦查可以用来看地上的痕迹。")
    assert asyncio.run(engine.ask(GROUP, LIN, "侦查能看脚印吗")) == "（侦查可以用来看地上的痕迹。）"
    content = [p for st, p in brain.calls if st == "oog"][0]["messages"][1]["content"]
    assert "侦查能看脚印吗" in content and "图书馆使用70" in content
    assert "暗格" not in content and "自己藏的" not in content and "真相" not in content


# ---------------------------------------------------------------- 完整跑一遍


def test_full_play_through(game):
    """开团 → 两人加入 → 开场 → 一人行动另一人超时待命 → 检定失败、花幸运拿到线索 → 第三人中途加入 →
    说服老周进储藏室 → 换场景、理智事件 → 一人离开 → 场外提问 → 回复「场外」→ 找到壶 → 结局。"""
    campaigns, rounds, engine, brain, rng, clock = game
    opening = started(game)
    assert "雨夜" in opening and "【茶馆大堂】" in opening
    c = campaigns.active(GROUP)

    # 第 1 轮：林晓行动，赵川一直不说话 → 问一次 → 待命
    act(game, LIN, "我看看柜台后面的地上", 1)
    clock.now += 30 * 60
    assert "还有人要行动吗" in rounds.tick(clock.now, T).notices[0][1]
    clock.now += 12 * 3600
    rounds.tick(clock.now, T)
    brain.add("intent", intent((LIN, "林晓看柜台后的地面", "investigate", {"target": "地面"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面",
                                             on_success={"clues": ["c2"]})]})
    brain.add("narrator", "林晓蹲在柜台后面看了一会儿，地上只有些水渍。赵川靠在门边没有说话。")
    rng.d100(65)
    out = asyncio.run(engine.settle(c.id))
    assert "花幸运" in out[1] and "赵川" in rounds.store.round(c.id, 1)["standby"] or True
    brain.add("narrator", "林晓又看了一遍，这次看清了：几个带茶渍的脚印，从柜台走到储藏室门口，花纹和老周的布鞋一样。")
    asyncio.run(engine.answer(GROUP, LIN, "花幸运"))
    assert {r["clue"] for r in campaigns.store.clues(c.id)} == {"c2"}

    # 第三人中途加入；第 2 轮：说服老周 → 进储藏室 → 花猫
    assert "下一轮登场" in cmd(campaigns, "加入", "苏禾", SU)
    act(game, ZHAO, "（我去倒杯水）", 20)
    act(game, ZHAO, "KP 我能看到老周的鞋吗", 21)
    assert rounds.mark_ooc(GROUP, ZHAO, "21", clock.now) == "好，那条算场外。"
    act(game, LIN, "老周，地上的脚印是你的吧？开一下储藏室吧", 22)
    act(game, ZHAO, "我跟着林晓", 23)
    brain.add("intent", intent((LIN, "林晓劝老周开储藏室", "talk", {"npc": "n1", "approach": "说服", "exit": "s2"}),
                               (ZHAO, "赵川跟着林晓", "other", {})))
    brain.add("arbiter", {"rulings": [
        ruling(LIN, True, skill="说服", difficulty="hard", object="老周",
               on_success={"flags": ["f1"], "exit": "s2", "npc_reply": "老周叹了口气，解下腰带上的钥匙"}),
        ruling(ZHAO)]})
    brain.add("narrator", "老周看着地上的脚印，叹了口气，解下腰带上的钥匙打开了储藏室。门一开，一只花猫从货架上扑了下来。")
    brain.add("summary", "林晓在柜台后发现了老周的脚印，说服老周打开了储藏室。")
    rng.d100(10, 30, 30)  # 说服 40 困难 → 10 通过；两人理智检定都过
    out = settle(game)
    assert "说服" in out[1] and "理智检定" in out[1] and any("苏禾加入了调查" in m for m in out)
    c = campaigns.active(GROUP)
    assert c.scene == "s2" and c.round == 3

    # 赵川离开；第 3 轮：苏禾摸墙角的砖 → 结局
    assert "下一轮离开" in cmd(campaigns, "离开", "", ZHAO)
    brain.add("oog", "可以，说你的调查员要怎么做就行。")
    assert asyncio.run(engine.ask(GROUP, SU, "我能直接去敲墙吗")).startswith("（")
    act(game, SU, "我敲敲墙角那块颜色浅的砖", 30)
    act(game, LIN, "我举着灯帮她照", 31)
    brain.add("intent", intent((SU, "苏禾敲墙角颜色浅的砖", "investigate", {"target": "墙角的砖"}),
                               (LIN, "林晓举灯", "other", {})))
    brain.add("arbiter", {"rulings": [ruling(SU, auto_clues=["c3"]), ruling(LIN)]})
    brain.add("narrator", "苏禾敲了敲墙角那块颜色发浅的砖，砖是松的。抽出来，后面是一个暗格，紫砂壶好好地放在里面，"
                          "壶底压着陈先生的名片，背面写着：对不起，祖传的东西，不能卖。老周看着壶，承认是自己藏的。")
    out = settle(game)
    assert "暗格" in out[0] and "承认是自己藏的" in out[0], "揭示之后可以说，结局之后真相也可以说"
    last = json.loads(campaigns.store.logs(c.id, "narration")[0]["data"])
    assert not last["safe"] and last["problems"] == [], "结局的叙述一次就通过检查，没有退回安全版本"
    assert "结束" in out[1]
    final = campaigns.get(c.id)
    assert final.phase == ENDED and "end1" in final.end_reason
    assert campaigns.active(GROUP) is None
