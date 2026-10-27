"""统一帮助。运行：python -m pytest tests -q"""

import pytest

from mytest.plugins._shared.help import HelpRegistry, HelpTopic


def make() -> HelpRegistry:
    reg = HelpRegistry()
    reg.register(HelpTopic("dice", "投骰", summary=(".rd [面数]　投骰",), detail="投骰详细", admin=("/dice AI 开启",),
                           aliases=("骰娘",), order=10))
    reg.register(HelpTopic("draw", "画图", summary=("/画图 描述　画图", "/nai 描述"), order=20))
    return reg


def test_overview_lists_every_topic_in_order():
    text = make().overview(is_admin=False)
    assert text.index("【投骰】") < text.index("【画图】")
    assert ".rd [面数]" in text and "/画图 描述" in text
    assert "/dice AI" not in text, "admin commands are not in the overview"
    assert "管理员" in make().overview(is_admin=True)


def test_find_by_title_alias_key_and_command():
    reg = make()
    for query in ("投骰", "骰娘", "DICE", " dice ", ".rd", "/画图", "画图", "nai", "/nai"):
        assert reg.find(query) is not None, query
    assert reg.find("骰娘").key == "dice"
    assert reg.find("/nai").key == "draw"
    assert reg.find("不存在") is None
    assert "投骰" in reg.not_found("不存在")


def test_detail_admin_section_only_for_admins():
    reg = make()
    topic = reg.find("dice")
    assert "投骰详细" in reg.detail(topic, is_admin=False)
    assert "/dice AI" not in reg.detail(topic, is_admin=False)
    assert "/dice AI" in reg.detail(topic, is_admin=True)
    assert "/nai 描述" in reg.detail(reg.find("draw"), is_admin=False), "falls back to summary"


def test_dynamic_detail_and_availability():
    reg = HelpRegistry()
    state = {"on": True, "model": "a"}
    reg.register(HelpTopic("tts", "语音", summary=("/说 文字",), detail=lambda: f"模型 {state['model']}",
                           available=lambda: state["on"]))
    state["model"] = "drama-3"
    assert "drama-3" in reg.detail(reg.find("语音"), is_admin=False)
    state["on"] = False
    assert reg.find("语音") is None and "语音" not in reg.overview(is_admin=False)


def test_name_clash_rejected():
    reg = make()
    with pytest.raises(ValueError):
        reg.register(HelpTopic("other", "骰娘", summary=("x",)))

