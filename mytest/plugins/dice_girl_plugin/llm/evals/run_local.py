import json
import sys
from datetime import datetime
from pathlib import Path

import nonebot

nonebot.init(log_level="INFO")

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mytest.plugins.dice_girl_plugin.llm.prompts import (  # noqa: E402
    build_dice_prompt,
    build_dice_user_prompt,
)
from mytest.plugins.dice_girl_plugin.llm.response import parse_dice_reaction  # noqa: E402
from mytest.plugins.dice_girl_plugin.persona import persona_manager  # noqa: E402
from mytest.plugins.dice_girl_plugin.persona.schema import (  # noqa: E402
    BEIJING,
    GROUP,
    PRIVATE,
    duty_state,
    user_call_for,
)

CASES_PATH = Path(__file__).with_name("cases.json")


def _load_cases() -> dict:
    with CASES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _role(role_id: str) -> dict:
    return persona_manager.data["roles"][role_id]


def _assert_terms(text: str, terms: list[str], case_name: str, what: str) -> None:
    missing = [term for term in terms if term not in text]
    if missing:
        raise AssertionError(f"{case_name}: {what} missing terms: {missing}")


def _dump_response(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def check_dice_cases() -> None:
    for case in _load_cases().get("dice", []):
        prompt = build_dice_prompt(
            _role(case["role"]),
            case["favorability"],
            case["user_name"],
            case["roll"]["event"],
        )
        user_prompt = build_dice_user_prompt(case["favorability"], case["roll"])
        _assert_terms(prompt, case["expected_prompt_terms"], case["name"], "prompt")
        if case["roll"]["event"] not in user_prompt:
            raise AssertionError(f"{case['name']}: dice user prompt missing event")
        parsed = parse_dice_reaction(_dump_response(case["sample_response"]))
        if not parsed["reply"] or parsed["delta"] != 0:
            raise AssertionError(f"{case['name']}: invalid parsed dice response")
        _assert_terms(parsed["reply"], case.get("expected_reply_terms", []), case["name"], "reply")


def check_duty_state() -> None:
    rinne = _role("rinne")
    noon = datetime(2026, 1, 1, 12, tzinfo=BEIJING)
    night = datetime(2026, 1, 1, 23, tzinfo=BEIJING)
    early = datetime(2026, 1, 1, 3, tzinfo=BEIJING)

    assert not duty_state(rinne, GROUP, 100, night).off_duty, "group is always on duty"
    assert not duty_state(rinne, PRIVATE, 70, noon).off_duty
    assert duty_state(rinne, PRIVATE, 90, noon).off_duty
    assert duty_state(rinne, PRIVATE, 70, night).off_duty
    assert duty_state(rinne, PRIVATE, 70, early).off_duty, "night span crosses midnight"
    assert not duty_state(rinne, PRIVATE, 50, night).off_duty

    top, mid = rinne["stages"][-1], rinne["stages"][2]
    assert "呆货" in user_call_for(top, duty_state(rinne, PRIVATE, 90, noon), "您")
    assert user_call_for(top, duty_state(rinne, GROUP, 90, noon), "您") == "主人"
    assert "呆货" not in user_call_for(mid, duty_state(rinne, PRIVATE, 70, night), "您")

    # 私聊专属设定只在私聊的点评 prompt 里出现
    assert "怕痒" in build_dice_prompt(rinne, 50, "小明", "侦查", scene=PRIVATE)
    assert "怕痒" not in build_dice_prompt(rinne, 50, "小明", "侦查", scene=GROUP)


def run() -> None:
    check_dice_cases()
    check_duty_state()
    print("dice girl local evals passed")


if __name__ == "__main__":
    run()
