"""画图指令解析、tag 清洗、朗读文本处理。运行：python -m pytest tests -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mytest.plugins.ai_draw_plugin.prompting import clean_tags, has_chinese, join_prompt, parse_request
from mytest.plugins.tts_plugin.service import speakable


def test_parse_request():
    req = parse_request("横图 海边")
    assert (req.size, req.prompt) == ("横图", "海边")
    assert parse_request("方 cat").size == "方图"
    assert parse_request("海边 横图").size == "竖图"
    assert parse_request("").prompt == ""


def test_tags():
    assert has_chinese("1girl, 白发") and not has_chinese("1girl, white hair")
    assert clean_tags("Tags: 1girl，solo,\n white hair, 1girl, ") == "1girl, solo, white hair"
    assert clean_tags("```1girl, solo```") == "1girl, solo"
    assert join_prompt("best quality,", "1girl") == "best quality, 1girl"
    assert join_prompt("  ", "1girl") == "1girl"


def test_speakable():
    assert speakable("（轻轻鼓掌）不愧是您～✨") == "不愧是您"
    assert speakable("*叹气* 好吧【小声】") == "好吧"
