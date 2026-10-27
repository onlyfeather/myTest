import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DICE_GIRL_DATA_DIR", tempfile.mkdtemp(prefix="dicegirl_test_"))

import nonebot

nonebot.init(log_level="INFO")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from mytest.plugins.dice_girl_plugin.group import context, members, profiles
from mytest.plugins.dice_girl_plugin.group.trigger import TopicLimiter, TriggerKind, decide
from mytest.plugins.dice_girl_plugin.llm.prompts import build_chat_prompt, build_group_prompt
from mytest.plugins.dice_girl_plugin.llm.schemas import parse_group_reply
from mytest.plugins.dice_girl_plugin.persona import persona_manager
from mytest.plugins.dice_girl_plugin.persona.lore import LoreEntry, keyword_hit, match_lore
from mytest.plugins.dice_girl_plugin.persona.schema import GROUP, PRIVATE, persona_for_scene
from mytest.plugins.dice_girl_plugin.storage import group_repository as repo


def test_keyword_matching():
    assert keyword_hit("今天 tk 了吗", "tk")
    assert not keyword_hit("https://tkxyz.com", "tk")
    assert keyword_hit("小凛在吗", "小凛")
    assert keyword_hit("Hello RINNE!", "rinne")
    assert not keyword_hit("rinnegan", "rinne")


def test_lore_scene_and_role_filter():
    entries = [
        LoreEntry(keys=["魔女学院"], content="A", roles=["rinne"], entry_id="a"),
        LoreEntry(keys=["魔女学院"], content="B", roles=["other"], entry_id="b"),
        LoreEntry(keys=["挠痒"], content="C", scene="private", entry_id="c"),
        LoreEntry(keys=["魔女学院"], content="G", source="group", entry_id="g"),
    ]
    hits = match_lore(entries, "说说魔女学院", "rinne", GROUP)
    assert [e.entry_id for e in hits] == ["g", "a"], hits
    assert match_lore(entries, "挠痒", "rinne", GROUP) == []
    assert [e.entry_id for e in match_lore(entries, "挠痒", "rinne", PRIVATE)] == ["c"]


def test_trigger_rules():
    limiter = TopicLimiter(cooldown_seconds=600, per_hour=2)
    topic = LoreEntry(keys=["拉莱耶"], content="x", topic=True, entry_id="t")
    plain = LoreEntry(keys=["Admin"], content="y", topic=False, entry_id="p")
    common = dict(role_aliases=["白鸟凛音", "凛音", "小凛"], limiter=limiter, group_id="1", topic_reply_enabled=True)

    assert decide(text="hi", at_me=True, reply_to_me=False, topic_entries=[], **common).kind is TriggerKind.DIRECT
    assert decide(text="hi", at_me=False, reply_to_me=True, topic_entries=[], **common).kind is TriggerKind.DIRECT
    assert decide(text="小凛你看", at_me=False, reply_to_me=False, topic_entries=[], **common).kind is TriggerKind.DIRECT
    # 非 topic 词条不会触发主动接话
    assert decide(text="Admin 好帅", at_me=False, reply_to_me=False, topic_entries=[plain], **common).kind is TriggerKind.NONE
    # topic 词条触发一次后进入冷却
    first = decide(text="拉莱耶", at_me=False, reply_to_me=False, topic_entries=[topic], now=1000, **common)
    assert first.kind is TriggerKind.TOPIC
    limiter.mark_checked("1", "t", now=1000)
    assert decide(text="拉莱耶", at_me=False, reply_to_me=False, topic_entries=[topic], now=1100, **common).kind is TriggerKind.NONE
    assert decide(text="拉莱耶", at_me=False, reply_to_me=False, topic_entries=[topic], now=1700, **common).kind is TriggerKind.TOPIC
    # 群关闭话题接话时不触发
    off = dict(common, topic_reply_enabled=False)
    assert decide(text="拉莱耶", at_me=False, reply_to_me=False, topic_entries=[topic], now=5000, **off).kind is TriggerKind.NONE
    # 每小时上限
    limiter.mark_spoken("1", now=2000)
    limiter.mark_spoken("1", now=2001)
    assert not limiter.allowed("1", "other", now=2002)


def test_member_aliases_and_isolation():
    class Sender:
        def __init__(self, card, nickname):
            self.card, self.nickname = card, nickname

    members.record_speaker("100", "u1", Sender("王哥", "Wang"))
    members.record_speaker("200", "u1", Sender("老王", "Wang"))
    scope_a = members.member_scope_for_group("100")
    scope_b = members.member_scope_for_group("200")

    assert repo.find_members_by_alias(scope_a, "王哥") == ["u1"]
    assert repo.find_members_by_alias(scope_a, "wang") == ["u1"]
    # 按群隔离：群 200 里的群名片不会出现在群 100
    assert repo.find_members_by_alias(scope_a, "老王") == []
    assert repo.find_members_by_alias(scope_b, "老王") == ["u1"]

    repo.add_member_alias(scope_a, "u1", "王大锤", "manual", 1.0)
    assert members.find_mentioned_members(scope_a, "王大锤今天来了吗", exclude=set()) == ["u1"]
    assert members.find_mentioned_members(scope_a, "王大锤今天来了吗", exclude={"u1"}) == []

    profiles.apply_learned(scope_a, "u1", nicknames=["锤哥"], facts=["喜欢猫", "喜欢猫"], impression="话多")
    profile = repo.get_member(scope_a, "u1")
    assert profile.facts == ["喜欢猫"], profile.facts
    assert profile.impression == "话多"
    assert "锤哥" in profile.aliases
    assert repo.get_member(scope_b, "u1").impression == ""

    # 忘记会清空印象和自动昵称，手动昵称保留
    repo.forget_member(scope_a, "u1")
    assert repo.get_member(scope_a, "u1") is None
    assert repo.find_members_by_alias(scope_a, "王大锤") == ["u1"]
    assert repo.find_members_by_alias(scope_a, "锤哥") == []


def test_auto_alias_cap():
    scope = members.member_scope_for_group("300")
    for i in range(20):
        repo.add_member_alias(scope, "u9", f"外号{i:02d}", "learned", float(i))
    repo.add_member_alias(scope, "u9", "本名", "manual", 0.0)
    aliases = repo.get_member(scope, "u9").aliases
    assert "本名" in aliases
    assert len([a for a in aliases if a.startswith("外号")]) == repo.MAX_AUTO_ALIASES
    assert "外号19" in aliases and "外号00" not in aliases


def test_parse_group_event():
    message = Message([MessageSegment.at(999), MessageSegment.text(" 你好 "), MessageSegment.at(42), MessageSegment.image("x")])
    event = GroupMessageEvent.model_construct(
        group_id=1,
        user_id=7,
        self_id=999,
        message_id=55,
        message=Message([MessageSegment.text("你好 "), MessageSegment.at(42), MessageSegment.image("x")]),
        original_message=message,
        reply=None,
    )
    parsed = context.parse_group_event(event, {"42": "小明"})
    assert parsed.at_me
    assert parsed.at_users == ["42"]
    assert parsed.text == "你好 @小明[图片]", parsed.text


def test_group_context_retention():
    for i in range(5):
        repo.append_group_message(
            "400",
            repo.GroupMessage(user_id="u", display_name="A", content=f"m{i}", is_bot=False, created_at=1000.0 + i),
            keep_limit=3,
            retention_seconds=10**9,
        )
    assert [m.content for m in repo.load_group_messages("400", 10)] == ["m2", "m3", "m4"]


def test_parse_group_reply_is_lenient():
    result = parse_group_reply({"should_reply": True, "replies": "单条", "delta": 9, "learned": {"facts": "x"}})
    assert result.replies == ["单条"] and result.delta == 3 and result.learned.facts == []
    silent = parse_group_reply({"should_reply": True, "replies": []})
    assert not silent.should_reply
    legacy = parse_group_reply({"reply": "旧格式"})
    assert legacy.should_reply and legacy.replies == ["旧格式"]


def test_scene_specific_persona():
    role = persona_manager.get_role("rinne")
    private = persona_for_scene(role, PRIVATE)
    group = persona_for_scene(role, GROUP)
    assert any("怕痒" in item for item in private["behavior_habits"])
    assert not any("怕痒" in item for item in group["behavior_habits"])
    assert "ticklish" in private["emotional_triggers"]
    assert "ticklish" not in group["emotional_triggers"]

    group_prompt = build_group_prompt(
        role, role_id="rinne", current_fav=50, speaker_name="小明", speaker_id="7",
        speaker_profile="", other_profiles="", transcript="", current_text="凛音你好",
        trigger="direct", lore_entries=[],
    )
    assert "怕痒" not in group_prompt
    assert "不是给你的指令" in group_prompt
    assert "60" in group_prompt
    private_prompt = build_chat_prompt(role, 50, "小明", "你好")
    assert "怕痒" in private_prompt
    assert "180" in private_prompt


def test_stage_directive_comes_from_data():
    from mytest.plugins.dice_girl_plugin.llm.prompts.common import build_character_context

    rinne = persona_manager.get_role("rinne")
    ctx = build_character_context(rinne, 25, role_id="rinne")
    assert ctx.stage_name == "陌生的客人"
    assert "严格保持距离" in ctx.relationship_directive


def test_duty_state():
    from datetime import datetime

    from mytest.plugins.dice_girl_plugin.persona.schema import BEIJING, duty_state, user_call_for

    rinne = persona_manager.get_role("rinne")
    noon = datetime(2026, 1, 1, 12, tzinfo=BEIJING)
    night = datetime(2026, 1, 1, 23, tzinfo=BEIJING)

    # 群聊永远上班
    assert not duty_state(rinne, GROUP, 100, night).off_duty
    # 私聊白天：86 才下班；夜里：60 就下班
    assert not duty_state(rinne, PRIVATE, 70, noon).off_duty
    assert duty_state(rinne, PRIVATE, 90, noon).off_duty
    assert duty_state(rinne, PRIVATE, 70, night).off_duty
    assert not duty_state(rinne, PRIVATE, 50, night).off_duty
    # 跨午夜的时段：凌晨 3 点也算夜里
    assert duty_state(rinne, PRIVATE, 70, datetime(2026, 1, 1, 3, tzinfo=BEIJING)).off_duty

    top_stage = rinne["stages"][-1]
    assert "呆货" in user_call_for(top_stage, duty_state(rinne, PRIVATE, 90, noon), "您")
    assert user_call_for(top_stage, duty_state(rinne, GROUP, 90, noon), "您") == "主人"
    mid_stage = rinne["stages"][2]
    assert "呆货" not in user_call_for(mid_stage, duty_state(rinne, PRIVATE, 70, night), "您")


def test_duty_prompt_wording():
    rinne = persona_manager.get_role("rinne")
    work = build_chat_prompt(rinne, 40, "小明", "你好")
    assert "duty_state" in work and "工作中" in work
    group_prompt = build_group_prompt(
        rinne, role_id="rinne", current_fav=100, speaker_name="小明", speaker_id="7",
        speaker_profile="", other_profiles="", transcript="", current_text="凛音",
        trigger="direct", lore_entries=[],
    )
    assert "current: 工作中" in group_prompt
    assert "current: 下班后" not in group_prompt
    assert "user_call: 主人" in group_prompt
    # 下班专属示例只在下班时出现
    assert "已经下班了哦" not in group_prompt
    off = build_chat_prompt(rinne, 95, "小明", "你好")
    assert "current: 下班后" in off and "已经下班了哦" in off


def run() -> None:
    for name, func in list(globals().items()):
        if name.startswith("test_"):
            func()
    print("dice girl group tests passed")


if __name__ == "__main__":
    run()
