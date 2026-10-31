"""跑团的发送队列、卡关提示、摘要、叙述审查、WebUI 管理。运行：python -m pytest tests/test_trpg_extras.py -q"""

import asyncio
import json

import pytest

from mytest.plugins.trpg_plugin.ai import ModelClient, ModelError
from mytest.plugins.trpg_plugin.campaign import ENDED, Actor
from mytest.plugins.trpg_plugin.manage import CampaignManager, ModuleManager
from mytest.plugins.trpg_plugin.module import BUILTIN_DIR
from mytest.plugins.trpg_plugin.module.loader import strip_comments
from mytest.plugins.trpg_plugin.outbox import MAX_ATTEMPTS, Outbox, split
from mytest.plugins.trpg_plugin.play import Engine

from .trpg_helpers import EP, GROUP, LIN, act, intent, make_game, no_sleep, ruling, settle, started


@pytest.fixture
def game(tmp_path):
    g, store = make_game(tmp_path)
    yield g
    store.close()


def pending(store):
    return [r["text"] for r in store._all("SELECT text FROM outbox WHERE status = 'pending' ORDER BY id")]


# ---------------------------------------------------------------- 发送队列


def test_split_long_text():
    assert split("短") == ["短"]
    parts = split("a" * 10 + "\n" + "b" * 10, limit=15)
    assert parts == ["a" * 10, "b" * 10]
    assert split("x" * 40, limit=15) == ["x" * 15, "x" * 15, "x" * 10]
    assert split("\n\n") == []


def test_outbox_sends_in_order_and_retries(game):
    campaigns = game[0]
    box = Outbox(campaigns.store)
    box.put("1", ["第一条", "第二条"], 100)
    box.put("2", ["别的群"], 100)
    sent = []
    fail = {"on": True}

    async def send(group, text):
        if group == "1" and fail["on"]:
            raise RuntimeError("机器人掉线")
        sent.append((group, text))

    assert asyncio.run(box.flush(send, 100)) == 1
    assert sent == [("2", "别的群")], "群 1 第一条失败，第二条等着，不影响群 2"
    assert asyncio.run(box.flush(send, 105)) == 0, "10 秒后才重试"
    fail["on"] = False
    assert asyncio.run(box.flush(send, 111)) == 2
    assert sent[1:] == [("1", "第一条"), ("1", "第二条")]
    assert asyncio.run(box.flush(send, 999)) == 0, "发过的不重发"


def test_outbox_gives_up_after_max_attempts(game):
    campaigns = game[0]
    box = Outbox(campaigns.store)
    box.put("1", ["发不出去"], 0)

    async def send(group, text):
        raise RuntimeError("风控")

    now = 0
    for _ in range(MAX_ATTEMPTS):
        asyncio.run(box.flush(send, now))
        now += 10_000
    stats = campaigns.store.outbox_stats()
    assert stats.get("failed") == 1 and not stats.get("pending")


def test_round_messages_go_through_outbox(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓翻看登记簿", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    brain.add("narrator", "林晓翻开登记簿，看到陈先生的名字，里面夹着一张便条：壶，不卖。")
    act(game, LIN, "我看登记簿", 1)
    out = settle(game)
    assert pending(campaigns.store) == [m for m in out if m.strip()]


def test_busy_notice_queued_once(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", *[ModelError("挂了")] * 6)
    act(game, LIN, "我看登记簿", 1)
    settle(game)
    engine.retry_now(campaigns.active(GROUP).id)
    asyncio.run(engine.settle(campaigns.active(GROUP).id))
    assert pending(campaigns.store).count("（KP 去续杯了，稍等一下。过几分钟会自动接着来。）") == 1


# ---------------------------------------------------------------- 卡关提示


def _empty_round(game, mid):
    campaigns, rounds, engine, brain, rng, clock = game
    brain.add("intent", intent((LIN, "林晓四处看看", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN)]})
    brain.add("narrator", "林晓在大堂里转了一圈，没有发现什么特别的。")
    act(game, LIN, "我四处看看", mid)
    return settle(game)


def test_stuck_gives_idea_roll_and_hint(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    assert "灵感" not in _empty_round(game, 1)[1]
    assert "灵感" not in _empty_round(game, 2)[1]
    rng.d100(30)  # 智力 70，通过
    out = _empty_round(game, 3)
    assert "灵感检定" in out[1] and "老周说起钥匙的时候" in out[1]
    assert out[1].endswith("你们接下来要做什么？")
    # 刚给过提示，再过 3 轮才会再试；第二次给下一条
    for mid in (4, 5):
        assert "灵感" not in _empty_round(game, mid)[1]
    rng.d100(30)
    assert "柜台后面的地面好像比别处脏" in _empty_round(game, 6)[1]


def test_stuck_idea_roll_can_fail(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    _empty_round(game, 1)
    _empty_round(game, 2)
    rng.d100(90)
    out = _empty_round(game, 3)
    assert "没有想到什么" in out[1] and "钥匙" not in out[1]


def test_progress_resets_stuck_counter(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    _empty_round(game, 1)
    brain.add("intent", intent((LIN, "林晓翻看登记簿", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    brain.add("narrator", "林晓翻开登记簿，看到陈先生的名字，里面夹着一张便条：壶，不卖。")
    act(game, LIN, "我看登记簿", 10)
    settle(game)
    _empty_round(game, 11)
    assert "灵感" not in _empty_round(game, 12)[1], "第 2 轮拿到了线索，到第 4 轮还没满 3 轮"


# ---------------------------------------------------------------- 摘要


def test_summary_written_on_scene_change_and_used(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    c = campaigns.active(GROUP)
    with campaigns.store.transaction() as db:
        campaigns.store.set_flag(db, c.id, "f1", 1)
    brain.add("intent", intent((LIN, "林晓进储藏室", "move", {"exit": "s2"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, exit="s2")]})
    brain.add("narrator", "林晓推开储藏室的门，一只花猫从货架上扑了下来。")
    brain.add("summary", "林晓进了储藏室，被一只花猫吓了一跳。")
    rng.d100(30)  # 理智检定
    act(game, LIN, "我进储藏室", 1)
    settle(game)
    assert campaigns.store.latest_summary(c.id)["text"] == "林晓进了储藏室，被一只花猫吓了一跳。"
    summary_input = [p for st, p in brain.calls if st == "summary"][0]["messages"][1]["content"]
    assert "花猫" in summary_input and "自己藏的" not in summary_input

    _empty_round(game, 2)
    narrator_input = [p for st, p in brain.calls if st == "narrator"][-1]["messages"][1]["content"]
    assert "前情摘要" in narrator_input and "被一只花猫吓了一跳" in narrator_input


def test_summary_failure_does_not_break_round(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    c = campaigns.active(GROUP)
    with campaigns.store.transaction() as db:
        campaigns.store.set_flag(db, c.id, "f1", 1)
    brain.add("intent", intent((LIN, "林晓进储藏室", "move", {"exit": "s2"})))
    brain.add("arbiter", {"rulings": [ruling(LIN, exit="s2")]})
    brain.add("narrator", "林晓推开储藏室的门，一只花猫从货架上扑了下来。")
    brain.add("summary", *[ModelError("挂了")] * 3)
    rng.d100(30)
    act(game, LIN, "我进储藏室", 1)
    settle(game)
    assert campaigns.active(GROUP).round == 2 and campaigns.store.latest_summary(c.id) is None


# ---------------------------------------------------------------- 叙述审查


def _review_game(game, enabled=True):
    campaigns, rounds, engine, brain, rng, clock = game
    return Engine(campaigns, rounds, engine.client, lambda role: EP, rng, clock, review_enabled=lambda: enabled)


def test_review_rejects_paraphrased_leak(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    eng = _review_game(game)
    brain.add("intent", intent((LIN, "林晓看墙", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN)]})
    brain.add("narrator", "墙角那块砖后面好像是空的。", "林晓看了看墙，墙面有些斑驳。")
    brain.add("review", {"ok": False, "problems": ["暗示了砖后面有空间"]}, {"ok": True})
    act(game, LIN, "我看墙", 1)
    out = settle(game, eng)
    assert out[0] == "林晓看了看墙，墙面有些斑驳。"
    review_input = [p for st, p in brain.calls if st == "review"][0]["messages"][1]["content"]
    assert "藏进储藏室西墙角的暗格" in review_input, "审查能看到秘密"
    rewrite = [p for st, p in brain.calls if st == "narrator"][1]["messages"][-1]["content"]
    assert "暗示了砖后面有空间" in rewrite


def test_review_failure_counts_as_pass(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    eng = _review_game(game)
    brain.add("intent", intent((LIN, "林晓看墙", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN)]})
    brain.add("narrator", "林晓看了看墙，墙面有些斑驳。")
    brain.add("review", *[ModelError("挂了")] * 3)
    act(game, LIN, "我看墙", 1)
    assert settle(game, eng)[0] == "林晓看了看墙，墙面有些斑驳。"


def test_review_off_makes_no_call(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", intent((LIN, "林晓看墙", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN)]})
    brain.add("narrator", "林晓看了看墙。")
    act(game, LIN, "我看墙", 1)
    settle(game)
    assert not [st for st, _ in brain.calls if st == "review"]


# ---------------------------------------------------------------- WebUI：模组


def _module_json(**changes):
    data = json.loads(strip_comments((BUILTIN_DIR / "m_tea_house.json").read_text(encoding="utf-8")))
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def test_module_manager(game):
    campaigns = game[0]
    mm = ModuleManager(campaigns, lambda: 0)
    items = asyncio.run(mm.items())["items"]
    assert [(i["id"], i["badges"]) for i in items] == [("m_tea_house", ["自带"])]
    assert "1–3 人" in items[0]["detail"]

    with pytest.raises(ValueError, match="和自带的模组重复"):
        asyncio.run(mm.run("upload", "", _module_json()))
    with pytest.raises(ValueError, match="JSON 格式错误"):
        asyncio.run(mm.run("upload", "", "{坏"))
    bad = json.loads(_module_json(id="m_bad"))
    bad["scenes"][0]["exits"][0]["to"] = "s9"
    with pytest.raises(ValueError, match="场景 s9 不存在"):
        asyncio.run(mm.run("upload", "", json.dumps(bad, ensure_ascii=False)))

    assert "已导入「茶馆（副本）」" in asyncio.run(mm.run("upload", "", _module_json(id="m_copy", title="茶馆（副本）")))
    assert asyncio.run(mm.run("check", "m_copy", None)) == "校验通过"
    with pytest.raises(ValueError, match="自带的模组不能删除"):
        asyncio.run(mm.run("delete", "m_tea_house", None))

    campaigns.open(GROUP, "茶馆（副本）", Actor(LIN))
    with pytest.raises(ValueError, match="有团正在用"):
        asyncio.run(mm.run("delete", "m_copy", None))
    campaigns.end(GROUP, Actor(LIN))
    assert asyncio.run(mm.run("delete", "m_copy", None)) == "已删除"


# ---------------------------------------------------------------- WebUI：进行中的团


def test_campaign_manager(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game)
    cm = CampaignManager(engine)
    c = campaigns.active(GROUP)
    (item,) = asyncio.run(cm.items())["items"]
    assert item["id"] == str(c.id) and "群 1130489499 · 第 1 轮 · 茶馆大堂 · 收集行动" in item["detail"]
    assert item["values"]["edit"] == ["林晓 理智55 幸运50", "赵川 理智60 幸运45"]

    view = asyncio.run(cm.run("state", str(c.id), None))
    assert "c3【关键】" in view and "未揭示" in view and "真相" in view and "自己藏的" in view

    before = campaigns.get(c.id).revision
    assert "已揭示 c3" in asyncio.run(cm.run("reveal", str(c.id), "c3"))
    assert campaigns.store.flags(c.id) == {"f2"}, "线索自带的标记一起设"
    assert campaigns.get(c.id).revision > before, "改了状态，正在等的模型回复作废"
    with pytest.raises(ValueError, match="已经揭示过"):
        asyncio.run(cm.run("reveal", str(c.id), "c3"))
    assert "已收回 c3" in asyncio.run(cm.run("hide", str(c.id), "c3"))
    with pytest.raises(ValueError, match="没有线索 c9"):
        asyncio.run(cm.run("reveal", str(c.id), "c9"))

    assert "林晓 san=40" in asyncio.run(cm.run("edit", str(c.id), ["林晓 理智40 幸运50"]))
    assert campaigns.player(c.id, LIN).san == 40
    with pytest.raises(ValueError, match="开头要写调查员名"):
        asyncio.run(cm.run("edit", str(c.id), ["张三 理智10"]))

    assert "第0轮 叙述：十月末的雨夜" in asyncio.run(cm.run("log", str(c.id), None)), "开场白也记了"
    assert asyncio.run(cm.run("end", str(c.id), None)) == "已结束"
    assert campaigns.get(c.id).phase == ENDED
    assert "「茶馆失物」结束了。" in pending(campaigns.store)
    with pytest.raises(ValueError, match="已经结束"):
        asyncio.run(cm.run("state", str(c.id), None))


def test_campaign_log_shows_model_calls(game):
    campaigns, rounds, engine, brain, rng, clock = game
    started(game, (LIN, "林晓"))
    brain.add("intent", ModelError("超时"), intent((LIN, "林晓翻看登记簿", "investigate", {})))
    brain.add("arbiter", {"rulings": [ruling(LIN, auto_clues=["c1"])]})
    brain.add("narrator", "林晓翻开登记簿，看到陈先生的名字，里面夹着一张便条：壶，不卖。")
    act(game, LIN, "我看登记簿", 1)
    settle(game)
    log = CampaignManager(engine).recent_log(campaigns.active(GROUP).id)
    assert "intent · 主模型 m" in log and "失败：超时" in log and "叙述" in log
    assert "https://a/v1" not in log, "日志里不放接口地址和密钥"


def test_model_client_mode_follows_setting():
    mode = {"v": "schema"}
    client = ModelClient(lambda *_: None, sleep=no_sleep, mode=lambda: mode["v"])
    assert client.mode_for(EP[0]) == "schema"
    mode["v"] = "plain"
    assert client.mode_for(EP[0]) == "plain"
