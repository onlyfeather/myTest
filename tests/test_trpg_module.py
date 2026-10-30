"""跑团模组的格式和导入校验。运行：python -m pytest tests/test_trpg_module.py -q"""

import copy
import json

import pytest

from mytest.plugins.trpg_plugin.module import BUILTIN_DIR, builtin_modules, load_module_data, load_module_text
from mytest.plugins.trpg_plugin.module.loader import strip_comments
from mytest.plugins.trpg_plugin.rules.skills import base_value, canonical, is_known

TEA_HOUSE = BUILTIN_DIR / "m_tea_house.json"


@pytest.fixture
def data():
    return json.loads(strip_comments(TEA_HOUSE.read_text(encoding="utf-8")))


def errors(result):
    return [f"{p.where}：{p.message}" for p in result.errors]


def has_error(result, where, words):
    return any(p.where == where and words in p.message for p in result.errors), result.report()


def test_builtin_modules_are_valid():
    results = builtin_modules()
    assert "m_tea_house.json" in results
    for name, result in results.items():
        assert result.ok, f"{name}\n{result.report()}"
        assert not result.warnings, f"{name}\n{result.report()}"


def test_tea_house_content():
    m = load_module_text(TEA_HOUSE.read_text(encoding="utf-8")).module
    assert m.scene("s1").name == "茶馆大堂" and m.npc("n1").name == "掌柜老周"
    assert [c.id for c in m.clues_in("s1")] == ["c1", "c2"]
    assert m.clue("c1").how is None and m.clue("c2").how.skill == "侦查" and m.clue("c3").key
    assert m.events[0].sanity.fail == "1"
    assert m.npc("n1").social["说服"] == 50 and m.npc("n1").attitude == "friendly"
    lin = m.pregens[0]
    assert lin.sheet["图书馆使用"] == 70 and lin.sheet["理智"] == 55
    assert {"暗格", "自己藏的"} <= m.all_spoiler_words()


def test_conditions():
    m = load_module_text(TEA_HOUSE.read_text(encoding="utf-8")).module
    exit_ = m.scene("s1").exits[0]
    assert not exit_.requires.met(set(), set(), "s1")
    assert exit_.requires.met({"f1"}, set(), "s1")
    assert m.events[0].when.met(set(), set(), "s2") and not m.events[0].when.met(set(), set(), "s1")
    assert m.endings[0].when.met({"f2"}, set(), "s1")


def test_comments_and_bom():
    text = '\ufeff{"a": "x // not a comment", /* c */ "b": "y /* no */"} // tail\n'
    assert json.loads(strip_comments(text.lstrip("\ufeff"))) == {"a": "x // not a comment", "b": "y /* no */"}
    bad = load_module_text('{\n  // comment\n  "format": 1,,\n}')
    assert bad.module is None and "第 3 行" in bad.errors[0].where


def test_unknown_field_suggests_fix(data):
    data["scenes"][0]["exit"] = []
    result = load_module_data(data)
    assert not result.ok
    assert any(p.where == "scenes[s1].exit" and "exits" in p.message for p in result.errors), result.report()


def test_ids_must_be_meaningless_codes(data):
    data["flags"][0]["id"] = "f_found_teapot"
    assert has_error(load_module_data(data), "flags[f_found_teapot].id", "编号格式")


def test_duplicate_ids(data):
    data["clues"][1]["id"] = "c1"
    assert has_error(load_module_data(data), "clues[c1]", "重复")


def test_missing_references(data):
    data["scenes"][0]["exits"][0]["to"] = "s9"
    data["scenes"][0]["npcs"] = ["n7"]
    data["endings"][0]["when"] = {"flags_all": ["f9"]}
    result = load_module_data(data)
    assert has_error(result, "scenes[s1].exits[0].to", "s9")
    assert has_error(result, "scenes[s1].npcs", "n7")
    assert has_error(result, "endings[end1].when", "f9")


def test_unreachable_scene_with_key_clue(data):
    data["scenes"][0]["exits"] = []
    result = load_module_data(data)
    assert has_error(result, "scenes[s2]", "关键线索")


def test_unreachable_scene_without_key_clue_is_warning(data):
    data["scenes"].append({"id": "s3", "name": "阁楼", "public": "积灰的阁楼"})
    result = load_module_data(data)
    assert result.ok and any(p.where == "scenes[s3]" for p in result.warnings)


def test_ending_needing_unreachable_clue(data):
    data["scenes"][0]["exits"] = []
    data["clues"][2]["key"] = False
    data["endings"][0]["when"] = {"clues_all": ["c3"]}
    result = load_module_data(data)
    assert has_error(result, "endings[end1].when", "c3")
    assert has_error(result, "endings", "没有一个结局")


def test_spoiler_word_in_public_text(data):
    data["scenes"][0]["public"] += "墙里像是有个暗格。"
    assert has_error(load_module_data(data), "scenes[s1].public", "暗格")


def test_spoiler_word_too_short(data):
    data["clues"][2]["spoiler_words"] = ["砖"]
    assert has_error(load_module_data(data), "clues[c3].spoiler_words", "太短")


def test_missing_spoiler_words_warn(data):
    data["clues"][2]["spoiler_words"] = []
    data["npcs"][0]["spoiler_words"] = []
    result = load_module_data(data)
    assert result.ok
    where = {p.where for p in result.warnings}
    assert {"clues[c3]", "npcs[n1]"} <= where


def test_key_clue_only_by_check_warns(data):
    data["clues"][2]["how"] = {"skill": "侦查", "difficulty": "困难"}
    result = load_module_data(data)
    assert result.ok and result.module.clue("c3").how.difficulty == "hard"
    assert any(p.where == "clues[c3]" and "on_fail" in p.message for p in result.warnings)


def test_bad_skill_difficulty_and_sanity(data):
    data["clues"][1]["how"] = {"skill": "侦察术", "difficulty": "很难"}
    data["events"][0]["sanity"] = {"success": "0", "fail": "一点"}
    data["npcs"][0]["social"]["说服"] = 150
    result = load_module_data(data)
    assert has_error(result, "clues[c2].how.skill", "侦察术")
    assert has_error(result, "clues[c2].how.difficulty", "regular")
    assert has_error(result, "events[e1].sanity.fail", "骰子")
    assert has_error(result, "npcs[n1].social.说服", "1 到 100")


def test_events_and_endings_need_conditions(data):
    data["events"][0]["when"] = {}
    del data["endings"][0]["when"]
    result = load_module_data(data)
    assert has_error(result, "events[e1].when", "触发条件")
    assert has_error(result, "endings[end1].when", "达成条件")


def test_pregen_sheets(data):
    data["pregens"][0]["sheet"] = "侦查60 理智55"
    data["pregens"][1]["sheet"] = {"锁匠": 70, "理智": 60, "智力": 60, "幸运": 45, "钓鱼": 30}
    data["pregens"][2]["sheet"] = "侦查60 理智-5 智力70 幸运50"
    result = load_module_data(data)
    assert has_error(result, "pregens[林晓].sheet", "智力")
    assert has_error(result, "pregens[苏禾].sheet", "加减")
    assert any(p.where == "pregens[赵川].sheet.钓鱼" for p in result.warnings)


def test_players_and_top_level(data):
    data["players"] = [3, 9]
    data["format"] = 2
    data["id"] = "茶馆"
    result = load_module_data(data)
    assert {"players", "format", "id"} <= {p.where for p in result.errors}


def test_not_an_object():
    assert not load_module_text("[]").ok
    assert not load_module_text('"x"').ok


def test_original_data_untouched(data):
    before = copy.deepcopy(data)
    load_module_data(data)
    assert data == before


def test_skill_names():
    assert canonical("图书馆") == "图书馆使用"
    assert canonical("san") == "理智" and canonical("侦察") == "侦查"
    assert is_known("灵感") and is_known("说服") and not is_known("钓鱼")
    assert base_value("闪避", {"敏捷": 60}) == 30
    assert base_value("母语", {"教育": 70}) == 70
    assert base_value("侦查", {}) == 25 and base_value("钓鱼", {}) == 0
