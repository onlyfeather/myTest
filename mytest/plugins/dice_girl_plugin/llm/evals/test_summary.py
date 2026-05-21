import json
import sys
from pathlib import Path

import nonebot

nonebot.init(log_level="INFO")

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mytest.plugins.dice_girl_plugin.llm.response import clean_and_parse_json  # noqa: E402
from mytest.plugins.dice_girl_plugin.llm.summary_service import (  # noqa: E402
    build_summary_prompt,
)


def run() -> None:
    prompt = build_summary_prompt(
        "用户喜欢安静陪伴。",
        [
            {"role": "user", "content": "我今天想继续侦查。"},
            {"role": "assistant", "content": "别逞强，我会看着你的。"},
        ],
    )
    for term in ["长期记忆摘要器", "不要超过 500 个中文字符", "summary"]:
        assert term in prompt

    parsed = clean_and_parse_json(
        json.dumps({"summary": "用户喜欢安静陪伴，并多次提到侦查。"}, ensure_ascii=False)
    )
    assert parsed["summary"].startswith("用户喜欢")
    print("dice girl summary eval passed")


if __name__ == "__main__":
    run()
