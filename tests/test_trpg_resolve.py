"""跑团每轮结算的前半段：①理解行动 ②裁决 ③校验 ④掷骰和状态更新。

用假模型（按顺序返回准备好的 JSON）和固定的骰子，跑茶馆失物。
运行：python -m pytest tests/test_trpg_resolve.py -q
"""

import asyncio
import json
import random

import pytest

from mytest.plugins.trpg_plugin.ai import AllFailed, Endpoint, ModelClient, ModelError, Reply, Stale, check_schema
from mytest.plugins.trpg_plugin.campaign import Actor, Campaigns, ModuleLibrary
from mytest.plugins.trpg_plugin.prompts import load as prompt
from mytest.plugins.trpg_plugin.resolve import (
    ResolveDeps,
    arbiter_schema,
    intent_schema,
    kp_context,
    resolve_round,
    situation,
    validate_rulings,
)
from mytest.plugins.trpg_plugin.store import Store

GROUP = "1130489499"
LIN, ZHAO = "1001", "1002"
EP = [Endpoint("主模型", "https://a/v1", "k", "m")]


class Fixed(random.Random):
    def __init__(self, *values):
        super().__init__(0)
        self.values = list(values)

    def randint(self, a, b):
        value = self.values.pop(0)
        assert a <= value <= b, (a, b, value)
        return value


def d100(*values):
    """依次掷出这些 D100（每个先个位再十位）。"""
    out = []
    for v in values:
        v %= 100
        out += [v % 10, v // 10]
    return out


class Script:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    async def __call__(self, endpoint, payload):
        self.calls.append(payload)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return Reply(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))


async def no_sleep(_):
    return None


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "trpg.db")
    campaigns = Campaigns(store, ModuleLibrary(store), lambda: 1_700_000_000)
    campaigns.open(GROUP, "茶馆失物", Actor(LIN))
    campaigns.join(GROUP, Actor(LIN), pregen="林晓")
    campaigns.join(GROUP, Actor(ZHAO), pregen="赵川")
    campaigns.start(GROUP, Actor(LIN))
    yield campaigns
    store.close()


def run(campaigns, script, actions, *dice, standby=(), still_current=None):
    deps = ResolveDeps(ModelClient(script, sleep=no_sleep), EP, Fixed(*dice), clock=lambda: 1_700_000_100)
    cid = campaigns.active(GROUP).id
    return asyncio.run(resolve_round(campaigns, cid, actions, standby, deps, still_current=still_current))


def intent(*actions):
    return {"actions": list(actions)}


def act(user, summary, kind="investigate", **kw):
    return {"user": user, "summary": summary, "kind": kind, **kw}


def ruling(user, needs_roll=False, **kw):
    return {"user": user, "needs_roll": needs_roll, **kw}


# ---------------------------------------------------------------- 提示词和资料


def test_prompts_load_without_header():
    for name in ("intent", "arbiter", "push"):
        text = prompt(name)
        assert text and "<!--" not in text and "autokeeper" not in text


def test_kp_context_has_secrets_and_ids(env):
    s = situation(env, env.active(GROUP).id, {LIN: "我看柜台"}, ())
    text = kp_context(s)
    for part in ("真相", "自己藏的", "c1", "c2", "【关键】" if False else "c2", "n1 掌柜老周", "说服50", "f1",
                 "条件还没满足"):
        assert part in text, part
    assert "c3" not in text, "别的场景的线索不给"


def test_schemas_restrict_ids(env):
    m = env.module(env.active(GROUP))
    s = intent_schema(m, "s1", [LIN, ZHAO])
    item = s["properties"]["actions"]["items"]["properties"]
    assert item["user"]["enum"] == [LIN, ZHAO] and item["npc"]["enum"] == ["", "n1"] and item["exit"]["enum"] == ["", "s2"]
    a = arbiter_schema(m, "s1", [LIN], {"c1"})
    clues = a["properties"]["rulings"]["items"]["properties"]["auto_clues"]["items"]["enum"]
    assert clues == ["c2"], "已揭示的不能再给"


# ---------------------------------------------------------------- 直接给的线索


def test_auto_clue_without_roll(env):
    script = Script(
        intent(act(LIN, "林晓翻看柜台上的登记簿", target="登记簿")),
        {"rulings": [ruling(LIN, auto_clues=["c1"], minutes=5)]},
    )
    res, record = run(env, script, {LIN: "我翻翻柜台上的登记簿"})
    (o,) = res.outcomes
    assert o.clues == ["c1"] and o.check is None and o.passed is None
    c = env.active(GROUP)
    assert {r["clue"] for r in env.store.clues(c.id)} == {"c1"}
    assert record["intent"]["actions"][0]["summary"] == "林晓翻看柜台上的登记簿"
    assert "我翻翻柜台上的登记簿" in script.calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------- 检定


def test_check_success_gives_clue(env):
    script = Script(
        intent(act(LIN, "林晓仔细看柜台后面的地面", target="柜台后的地面")),
        {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="柜台后的地面",
                            why="看你能不能注意到地上的痕迹", on_success={"clues": ["c2"]})]},
    )
    res, _ = run(env, script, {LIN: "我看看柜台后面的地上"}, *d100(42))
    (o,) = res.outcomes
    assert o.check.roll == 42 and o.check.value == 60 and o.passed and o.clues == ["c2"]
    rolls = env.store.rolls(env.active(GROUP).id)
    assert [(r["skill"], r["roll"], r["passed"], r["object"]) for r in rolls] == [("侦查", 42, 1, "柜台后的地面")]


def test_check_failure_gives_nothing_and_offers_push_and_luck(env):
    script = Script(
        intent(act(LIN, "林晓看地面", target="地面")),
        {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面", push_risk="动静太大被老周注意到",
                            on_success={"clues": ["c2"]})]},
    )
    res, _ = run(env, script, {LIN: "我看看地上"}, *d100(65))
    (o,) = res.outcomes
    assert not o.passed and o.clues == [] and o.can_push and o.luck_cost == 5
    assert o.push_risk == "动静太大被老周注意到"
    assert env.store.clues(env.active(GROUP).id) == []


def test_skill_not_on_sheet_uses_base_value(env):
    script = Script(
        intent(act(ZHAO, "赵川查登记簿里的记录")),
        {"rulings": [ruling(ZHAO, True, skill="图书馆", difficulty="regular", object="登记簿")]},
    )
    res, _ = run(env, script, {ZHAO: "我查记录"}, *d100(15))
    assert res.outcomes[0].check.skill == "图书馆使用" and res.outcomes[0].check.value == 20


# ---------------------------------------------------------------- ③ 校验把错误给模型重来


def test_invalid_rulings_are_retried_with_errors(env):
    bad = {"rulings": [ruling(LIN, auto_clues=["c2"])]}  # c2 要检定
    good = {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面", on_success={"clues": ["c2"]})]}
    script = Script(intent(act(LIN, "林晓看地面")), bad, good)
    res, _ = run(env, script, {LIN: "我看地面"}, *d100(10))
    assert res.outcomes[0].clues == ["c2"]
    retry = script.calls[2]["messages"][-1]["content"]
    assert "线索 c2 需要检定" in retry


@pytest.mark.parametrize("bad, message", [
    ({"rulings": [ruling(LIN, auto_clues=["c3"])]}, "只能是"),  # schema 的 enum 先挡住
    ({"rulings": [ruling(LIN, exit="s2")]}, "出口条件还没满足"),
    ({"rulings": [ruling(LIN, True, skill="钓鱼", difficulty="regular")]}, "不是 CoC 7 版的技能"),
    ({"rulings": [ruling(LIN, True, difficulty="regular")]}, "要写技能"),
    ({"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", auto_clues=["c1"])]}, "写在 on_success"),
    ({"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", on_fail={"clues": ["c2"]})]}, "失败不送线索"),
    ({"rulings": [ruling(LIN, on_success={"clues": ["c1"]})]}, "不检定就不要写"),
    ({"rulings": []}, "至少要 1 项"),
    ({"rulings": [ruling(ZHAO)]}, "缺少玩家 1001"),
])
def test_validation_rules(env, bad, message):
    s = situation(env, env.active(GROUP).id, {LIN: "x"}, ())
    errors = check_schema(bad, arbiter_schema(s.module, "s1", [LIN, ZHAO], s.revealed))
    if not errors:
        errors = validate_rulings(s, {LIN: {"user": LIN}})(bad)
    assert any(message in e for e in errors), errors


def test_code_rejects_clues_from_other_scenes_even_without_schema(env):
    # 接口不支持 schema、只靠提示词时，enum 不生效，要靠代码挡住
    s = situation(env, env.active(GROUP).id, {LIN: "x"}, ())
    errors = validate_rulings(s, {LIN: {}})({"rulings": [ruling(LIN, auto_clues=["c3"])]})
    assert any("不在当前场景" in e for e in errors), errors


def test_exit_allowed_when_flag_set_in_same_ruling(env):
    s = situation(env, env.active(GROUP).id, {LIN: "x"}, ())
    ok = {"rulings": [ruling(LIN, True, skill="说服", difficulty="hard", on_success={"flags": ["f1"], "exit": "s2"})]}
    assert validate_rulings(s, {LIN: {"user": LIN}})(ok) == []


def test_same_clue_to_two_people_rejected(env):
    s = situation(env, env.active(GROUP).id, {LIN: "x", ZHAO: "y"}, ())
    bad = {"rulings": [ruling(LIN, auto_clues=["c1"]), ruling(ZHAO, auto_clues=["c1"])]}
    assert any("同时给了两个人" in e for e in validate_rulings(s, {LIN: {}, ZHAO: {}})(bad))


def test_repeat_check_rejected_until_something_changes(env):
    first = Script(intent(act(LIN, "林晓看地面")),
                   {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面")]})
    run(env, first, {LIN: "我看地面"}, *d100(90))
    s = situation(env, env.active(GROUP).id, {LIN: "再看"}, ())
    again = {"rulings": [ruling(LIN, True, skill="侦查", difficulty="regular", object="地面")]}
    assert any("重复" in e for e in validate_rulings(s, {LIN: {}})(again))
    other_skill = {"rulings": [ruling(LIN, True, skill="聆听", difficulty="regular", object="地面")]}
    assert validate_rulings(s, {LIN: {}})(other_skill) == []
    s2 = situation(env, env.active(GROUP).id, {ZHAO: "我来看"}, ())
    assert validate_rulings(s2, {ZHAO: {}})({"rulings": [ruling(ZHAO, True, skill="侦查", difficulty="regular",
                                                                  object="地面")]}) == [], "换一个人可以"


# ---------------------------------------------------------------- 社交、标记、移动、事件、结局


def test_persuade_npc_sets_flag_moves_and_triggers_sanity_event(env):
    script = Script(
        intent(act(LIN, "林晓劝老周打开储藏室", "talk", npc="n1", approach="说服", exit="s2")),
        {"rulings": [ruling(LIN, True, skill="说服", difficulty="hard", object="老周",
                            on_success={"flags": ["f1"], "exit": "s2", "npc_reply": "老周叹气，解下腰带上的钥匙"},
                            on_fail={"npc_reply": "老周说钥匙丢了"})]},
    )
    # 说服 40 困难要 20 以下：掷 15 通过；理智检定：林晓 30 通过、赵川 80 失败扣 1
    res, _ = run(env, script, {LIN: "老周，开一下储藏室吧"}, *d100(15), *d100(30), *d100(80))
    (o,) = [x for x in res.outcomes if x.user_id == LIN]
    assert o.passed and o.flags == ["f1"] and o.moved_to == "s2" and o.npc_reply.startswith("老周叹气")
    assert (res.scene_before, res.scene_after) == ("s1", "s2")
    c = env.active(GROUP)
    assert c.scene == "s2" and env.store.flags(c.id) == {"f1"}
    (ev,) = res.events
    assert ev.event_id == "e1" and "花猫" in ev.public and set(ev.sanity) == {LIN, ZHAO}
    assert env.player(c.id, LIN).san == 55 and env.player(c.id, ZHAO).san == 59
    assert env.store.fired_events(c.id) == {"e1"}
    assert [r["kind"] for r in env.store.rolls(c.id)] == ["check", "sanity", "sanity"]
    assert any(r["npc"] == "n1" and r["appeared"] for r in env.store.npcs(c.id))


def test_once_event_does_not_fire_twice(env):
    c = env.active(GROUP)
    with env.store.transaction() as db:
        env.store.set_flag(db, c.id, "f1", 1)
        env.store.update_campaign(db, c.id, 0, scene="s2")
        env.store.fire_event(db, c.id, "e1", 1)
    script = Script(intent(act(LIN, "林晓看货架")), {"rulings": [ruling(LIN)]})
    res, _ = run(env, script, {LIN: "我看货架"})
    assert res.events == []


def test_key_clue_sets_flag_and_reaches_ending(env):
    c = env.active(GROUP)
    with env.store.transaction() as db:
        env.store.update_campaign(db, c.id, 0, scene="s2")
        env.store.fire_event(db, c.id, "e1", 1)
    script = Script(intent(act(ZHAO, "赵川摸西墙角颜色发浅的砖", target="墙角的砖")),
                    {"rulings": [ruling(ZHAO, auto_clues=["c3"])]})
    res, _ = run(env, script, {ZHAO: "我敲敲墙角那块浅色的砖"})
    assert res.outcomes[0].clues == ["c3"] and res.outcomes[0].flags == ["f2"], "线索自带的标记"
    assert res.ending == "end1"


def test_secret_told(env):
    script = Script(
        intent(act(LIN, "林晓追问老周", "talk", npc="n1", approach="说服")),
        {"rulings": [ruling(LIN, True, skill="说服", difficulty="hard", object="老周",
                            on_success={"npc_reply": "老周承认壶是他藏的", "secret_told": True})]},
    )
    res, _ = run(env, script, {LIN: "老周，你是不是有事瞒着我们"}, *d100(5))
    assert res.outcomes[0].secret_told
    assert [r["npc"] for r in env.store.npcs(env.active(GROUP).id) if r["secret_told"]] == ["n1"]


def test_standby_players_listed(env):
    script = Script(intent(act(LIN, "林晓看登记簿")), {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    res, _ = run(env, script, {LIN: "我看登记簿"}, standby=(ZHAO,))
    assert [(o.name, o.standby) for o in res.outcomes] == [("林晓", False), ("赵川", True)]
    assert ZHAO not in script.calls[0]["messages"][1]["content"].split("玩家原话")[0][-20:]


def test_everyone_standby_needs_no_model(env):
    res, record = run(env, Script(), {}, standby=(LIN, ZHAO))
    assert all(o.standby for o in res.outcomes) and record == {"intent": None, "rulings": None}


# ---------------------------------------------------------------- 失败时不改数据库


def test_all_models_failed_changes_nothing(env):
    c = env.active(GROUP)
    before = (c.revision, env.store.rolls(c.id), env.store.clues(c.id))
    with pytest.raises(AllFailed):
        run(env, Script(*[ModelError("挂了")] * 3), {LIN: "我看登记簿"})
    c2 = env.active(GROUP)
    assert (c2.revision, env.store.rolls(c.id), env.store.clues(c.id)) == before


def test_stale_changes_nothing(env):
    script = Script(intent(act(LIN, "林晓看登记簿")), {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    with pytest.raises(Stale):
        run(env, script, {LIN: "我看登记簿"}, still_current=lambda: len(script.items) > 0)
    assert env.store.clues(env.active(GROUP).id) == []
