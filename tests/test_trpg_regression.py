"""跑团提示词的回归用例：用每个用例自带的假回复跑一遍，确认用例、断言和请求的拼法没问题。

真模型跑（在服务器上）：python -m mytest.plugins.trpg_plugin.regression
运行：python -m pytest tests/test_trpg_regression.py -q
"""

import asyncio

import pytest

from mytest.plugins.trpg_plugin.regression import load_cases, run_fake

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case_passes_with_fake_reply(case):
    result = asyncio.run(run_fake(case))
    assert result.ok, result.failures


def test_assertions_catch_bad_output():
    """同一个用例换成不合格的回复，断言要能抓到。"""
    bad = {
        "越权声明：说成已经找到了，按尝试处理": {"actions": [{"user": "1001", "summary": "林晓在柜台里找到了钥匙",
                                                   "kind": "investigate"}]},
        "对 NPC 的社交检定按他的数值定难度（老周说服 50 → 困难）": {"rulings": [{
            "user": "1001", "needs_roll": True, "skill": "说服", "difficulty": "regular", "object": "老周"}]},
        "叙述：检定失败写成没找到": "林晓在地上看到几个带茶渍的脚印。",
        "场外提问：不透露剧情": "壶在储藏室西墙的暗格里。",
    }
    for case in CASES:
        if case["name"] in bad:
            result = asyncio.run(run_fake({**case, "fake": [bad[case["name"]]]}))
            assert not result.ok, case["name"]


def test_every_stage_covered():
    stages = {c["stage"] for c in CASES}
    assert stages == {"intent", "arbiter", "narrator", "oog"}
