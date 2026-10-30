"""跑团的数据表和团的状态机。运行：python -m pytest tests/test_trpg_campaign.py -q"""

import json

import pytest

from mytest.plugins.trpg_plugin.campaign import (
    ENDED,
    JOINING,
    LEAVING,
    LEFT,
    PAUSED,
    PRESENT,
    RECRUITING,
    RUNNING,
    Actor,
    CampaignError,
    Campaigns,
    ModuleLibrary,
)
from mytest.plugins.trpg_plugin.module import BUILTIN_DIR
from mytest.plugins.trpg_plugin.module.loader import strip_comments
from mytest.plugins.trpg_plugin.store import Store

GROUP = "1130489499"
OPENER = Actor("1001")
ALICE = Actor("1002")
BOB = Actor("1003")
CAROL = Actor("1004")
ADMIN = Actor("9999", is_manager=True)
MY_SHEET = {"侦查": 70, "san": 60, "灵感": 65, "幸运": 40, "体质": 50, "体型": 60}


class Clock:
    def __init__(self):
        self.now = 1_700_000_000

    def __call__(self):
        self.now += 1
        return self.now


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "trpg.db")
    yield s
    s.close()


@pytest.fixture
def campaigns(store):
    return Campaigns(store, ModuleLibrary(store), Clock())


def opened(campaigns):
    campaigns.open(GROUP, "茶馆失物", OPENER)
    return campaigns.active(GROUP)


def started(campaigns):
    opened(campaigns)
    campaigns.join(GROUP, OPENER, pregen="林晓")
    campaigns.join(GROUP, ALICE, pregen="赵川")
    campaigns.start(GROUP, OPENER)
    return campaigns.active(GROUP)


def test_open_by_title_or_id(campaigns):
    campaign, module = campaigns.open(GROUP, "茶馆失物", OPENER)
    assert campaign.phase == RECRUITING and campaign.opener == OPENER.user_id
    assert module.id == "m_tea_house" and campaign.module_title == "茶馆失物"
    campaigns.end(GROUP, OPENER)
    assert campaigns.open(GROUP, "m_tea_house", ALICE)[0].opener == ALICE.user_id


def test_open_errors(campaigns):
    with pytest.raises(CampaignError, match="茶馆失物"):
        campaigns.open(GROUP, "不存在的模组", OPENER)
    with pytest.raises(CampaignError, match="要说模组名"):
        campaigns.open(GROUP, "", OPENER)
    opened(campaigns)
    with pytest.raises(CampaignError, match="已经有一个招募中的团"):
        campaigns.open(GROUP, "茶馆失物", ALICE)
    assert campaigns.open("other", "茶馆失物", ALICE)[0].group_id == "other", "其他群互不影响"


def test_one_unfinished_campaign_per_group_in_database(store):
    store.insert_campaign(GROUP, "m", "t", "{}", "1", 0)
    with pytest.raises(Exception, match="UNIQUE"):
        store.insert_campaign(GROUP, "m", "t", "{}", "2", 0)


def test_join_with_pregen(campaigns):
    opened(campaigns)
    p = campaigns.join(GROUP, ALICE, pregen="林晓")
    assert (p.name, p.occupation, p.pregen, p.status) == ("林晓", "记者", "林晓", PRESENT)
    assert p.sheet["图书馆使用"] == 70 and p.san == 55 and p.luck == 50 and p.hp is None
    with pytest.raises(CampaignError, match="已经在团里"):
        campaigns.join(GROUP, ALICE, pregen="赵川")
    with pytest.raises(CampaignError, match="已经有人用了"):
        campaigns.join(GROUP, BOB, pregen="林晓")
    with pytest.raises(CampaignError, match="可选：林晓、赵川、苏禾"):
        campaigns.join(GROUP, BOB, pregen="张三")


def test_join_with_own_sheet(campaigns):
    opened(campaigns)
    p = campaigns.join(GROUP, ALICE, own_sheet=MY_SHEET, own_name="阿丽丝")
    assert p.name == "阿丽丝" and p.pregen == ""
    assert p.sheet["理智"] == 60 and p.sheet["智力"] == 65 and p.hp == 11, "别名统一，HP 从体质体型算"
    with pytest.raises(CampaignError, match="还没有角色卡"):
        campaigns.join(GROUP, BOB, own_sheet={}, own_name="鲍勃")
    with pytest.raises(CampaignError, match="缺少 智力、幸运"):
        campaigns.join(GROUP, BOB, own_sheet={"理智": 50}, own_name="鲍勃")
    with pytest.raises(CampaignError, match="已经有叫「阿丽丝」"):
        campaigns.join(GROUP, BOB, own_sheet=MY_SHEET, own_name="阿丽丝")


def test_player_limit(campaigns):
    opened(campaigns)  # 茶馆失物最多 3 人
    for actor, name in [(OPENER, "林晓"), (ALICE, "赵川"), (BOB, "苏禾")]:
        campaigns.join(GROUP, actor, pregen=name)
    with pytest.raises(CampaignError, match="最多 3 人"):
        campaigns.join(GROUP, CAROL, own_sheet=MY_SHEET, own_name="卡萝")


def test_leave_while_recruiting_removes_player(campaigns):
    opened(campaigns)
    campaigns.join(GROUP, ALICE, pregen="林晓")
    player, paused = campaigns.leave(GROUP, ALICE)
    assert player.name == "林晓" and not paused
    c = campaigns.active(GROUP)
    assert campaigns.player(c.id, ALICE.user_id) is None
    assert campaigns.join(GROUP, BOB, pregen="林晓").name == "林晓", "预设角色空出来了"
    with pytest.raises(CampaignError, match="不在这个团里"):
        campaigns.leave(GROUP, ALICE)


def test_start(campaigns, store):
    opened(campaigns)
    with pytest.raises(CampaignError, match="至少要 1 人"):
        campaigns.start(GROUP, OPENER)
    campaigns.join(GROUP, ALICE, pregen="林晓")
    with pytest.raises(CampaignError, match="开团的人或群管理员"):
        campaigns.start(GROUP, ALICE)
    c, module = campaigns.start(GROUP, OPENER)
    assert (c.phase, c.scene, c.round) == (RUNNING, "s1", 1)
    npcs = [tuple(r) for r in store.npcs(c.id)]
    assert npcs == [("n1", "friendly", "s1", 0, 0)]
    with pytest.raises(CampaignError, match="已经开始"):
        campaigns.start(GROUP, OPENER)


def test_manager_can_control(campaigns):
    started(campaigns)
    assert campaigns.pause(GROUP, ADMIN).phase == PAUSED
    assert campaigns.resume(GROUP, ADMIN).phase == RUNNING
    assert campaigns.end(GROUP, ADMIN, "管理员结束").phase == ENDED


def test_pause_resume_end_permissions(campaigns):
    started(campaigns)
    for op in (campaigns.pause, campaigns.end):
        with pytest.raises(CampaignError, match="开团的人或群管理员"):
            op(GROUP, ALICE)
    assert campaigns.pause(GROUP, OPENER).phase == PAUSED
    with pytest.raises(CampaignError, match="不能暂停"):
        campaigns.pause(GROUP, OPENER)
    with pytest.raises(CampaignError, match="开团的人或群管理员"):
        campaigns.resume(GROUP, ALICE)
    assert campaigns.resume(GROUP, OPENER).phase == RUNNING
    with pytest.raises(CampaignError, match="不用继续"):
        campaigns.resume(GROUP, OPENER)
    ended = campaigns.end(GROUP, OPENER)
    assert ended.phase == ENDED and ended.end_reason == "手动结束"
    assert campaigns.active(GROUP) is None
    with pytest.raises(CampaignError, match="没有团"):
        campaigns.pause(GROUP, OPENER)


def test_mid_game_join_and_leave_wait_for_next_round(campaigns):
    c = started(campaigns)
    bob = campaigns.join(GROUP, BOB, pregen="苏禾")
    assert bob.status == JOINING and bob.joined_round == 1
    _, paused = campaigns.leave(GROUP, ALICE)
    assert not paused
    assert campaigns.player(c.id, ALICE.user_id).status == LEAVING
    with pytest.raises(CampaignError, match="等下一轮"):
        campaigns.join(GROUP, ALICE, pregen="赵川")

    change = campaigns.settle_membership(c.id)
    assert [p.name for p in change.entered] == ["苏禾"]
    assert [p.name for p in change.exited] == ["赵川"]
    assert campaigns.player(c.id, BOB.user_id).status == PRESENT
    alice = campaigns.player(c.id, ALICE.user_id)
    assert alice.status == LEFT and alice.left_round == 1
    again = campaigns.settle_membership(c.id)
    assert not again.entered and not again.exited

    rejoined = campaigns.join(GROUP, ALICE, own_sheet=MY_SHEET, own_name="阿丽丝")
    assert rejoined.status == JOINING and rejoined.name == "阿丽丝" and rejoined.left_round is None


def test_joining_player_who_leaves_before_entering_is_removed(campaigns):
    c = started(campaigns)
    campaigns.join(GROUP, BOB, pregen="苏禾")
    campaigns.leave(GROUP, BOB)
    assert campaigns.player(c.id, BOB.user_id) is None
    assert not campaigns.settle_membership(c.id).entered


def test_last_player_leaving_pauses(campaigns):
    started(campaigns)
    campaigns.leave(GROUP, OPENER)
    _, paused = campaigns.leave(GROUP, ALICE)
    assert paused and campaigns.active(GROUP).phase == PAUSED
    with pytest.raises(CampaignError, match="没有调查员"):
        campaigns.resume(GROUP, OPENER)
    campaigns.join(GROUP, BOB, pregen="苏禾")
    assert campaigns.resume(GROUP, OPENER).phase == RUNNING


def test_revision_bumps_on_every_change(campaigns):
    c = opened(campaigns)
    revisions = [c.revision]
    steps = [
        lambda: campaigns.join(GROUP, OPENER, pregen="林晓"),
        lambda: campaigns.start(GROUP, OPENER),
        lambda: campaigns.join(GROUP, ALICE, pregen="赵川"),
        lambda: campaigns.settle_membership(c.id),
        lambda: campaigns.pause(GROUP, OPENER),
        lambda: campaigns.resume(GROUP, OPENER),
        lambda: campaigns.leave(GROUP, ALICE),
    ]
    for step in steps:
        step()
        revisions.append(campaigns.get(c.id).revision)
    assert revisions == sorted(set(revisions)), revisions


def test_module_snapshot_survives_module_changes(tmp_path, store):
    builtin = tmp_path / "modules"
    builtin.mkdir()
    source = (BUILTIN_DIR / "m_tea_house.json").read_text(encoding="utf-8")
    (builtin / "m_tea_house.json").write_text(source, encoding="utf-8")
    campaigns = Campaigns(store, ModuleLibrary(store, builtin), Clock())
    c, _ = campaigns.open(GROUP, "茶馆失物", OPENER)
    (builtin / "m_tea_house.json").unlink()

    fresh = Campaigns(store, ModuleLibrary(store, builtin), Clock())  # 相当于重启
    assert fresh.module(fresh.active(GROUP)).title == "茶馆失物"
    assert [x.id for x in fresh.unfinished()] == [c.id]


def test_state_survives_restart(tmp_path):
    path = tmp_path / "trpg.db"
    s1 = Store(path)
    c = started(Campaigns(s1, ModuleLibrary(s1), Clock()))
    s1.close()
    s2 = Store(path)
    again = Campaigns(s2, ModuleLibrary(s2), Clock())
    assert again.active(GROUP) == c
    assert [p.name for p in again.players(c.id)] == ["林晓", "赵川"]
    s2.close()


def test_transaction_rolls_back(store, campaigns):
    c = started(campaigns)
    before = campaigns.get(c.id)
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            store.set_flag(db, c.id, "f1", 1)
            store.update_campaign(db, c.id, 0, scene="s2")
            raise RuntimeError("boom")
    assert campaigns.get(c.id) == before and store.flags(c.id) == set()


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE campaign_npc (campaign_id INTEGER NOT NULL, npc TEXT NOT NULL, attitude TEXT NOT NULL,"
               " location TEXT NOT NULL DEFAULT '', appeared INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (campaign_id, npc))")
    db.execute("INSERT INTO campaign_npc VALUES (1, 'n1', 'neutral', 's1', 1)")
    db.commit()
    db.close()
    s = Store(path)
    assert [tuple(r) for r in s.npcs(1)] == [("n1", "neutral", "s1", 1, 0)]
    s.close()


def test_store_rejects_unknown_columns(store, campaigns):
    c = started(campaigns)
    with pytest.raises(ValueError):
        with store.transaction() as db:
            store.update_campaign(db, c.id, 0, revision=0)
    with pytest.raises(ValueError):
        with store.transaction() as db:
            store.update_player(db, c.id, OPENER.user_id, sheet="{}")


def test_flags_and_clues(store, campaigns):
    c = started(campaigns)
    with store.transaction() as db:
        store.set_flag(db, c.id, "f1", 1)
        store.set_flag(db, c.id, "f1", 2)
        assert store.reveal_clue(db, c.id, "c2", OPENER.user_id, 1)
        assert not store.reveal_clue(db, c.id, "c2", ALICE.user_id, 2), "已揭示的线索不会再记一次"
    assert store.flags(c.id) == {"f1"}
    assert [tuple(r) for r in store.clues(c.id)] == [("c2", OPENER.user_id, 1)]


def test_status_text(campaigns):
    assert "没有团" in campaigns.status_text(GROUP)
    opened(campaigns)
    campaigns.join(GROUP, OPENER, pregen="林晓")
    text = campaigns.status_text(GROUP)
    assert "招募中" in text and "林晓" in text and "赵川、苏禾" in text
    campaigns.start(GROUP, OPENER)
    campaigns.join(GROUP, ALICE, pregen="赵川")
    text = campaigns.status_text(GROUP)
    assert "第 1 轮，在茶馆大堂" in text and "赵川（下一轮登场）" in text
    assert "暗格" not in text and "s1" not in text


def test_library_upload_and_delete(store):
    library = ModuleLibrary(store)
    data = json.loads(strip_comments((BUILTIN_DIR / "m_tea_house.json").read_text(encoding="utf-8")))
    result = library.upload(json.dumps(data, ensure_ascii=False), 0)
    assert not result.ok and "自带的模组重复" in result.report()

    data["id"], data["title"] = "m_copy", "茶馆失物（副本）"
    assert library.upload(json.dumps(data, ensure_ascii=False), 0).ok
    assert [m.id for m, _, builtin in library.all() if not builtin] == ["m_copy"]
    assert library.find("茶馆失物（副本）")[0].id == "m_copy"

    data["scenes"][0]["exits"][0]["to"] = "s9"
    bad = library.upload(json.dumps(data, ensure_ascii=False), 1)
    assert not bad.ok and library.find("m_copy")[0].scene("s1").exits[0].to == "s2", "校验不过不覆盖"

    assert library.delete("m_copy") and not library.delete("m_copy")
