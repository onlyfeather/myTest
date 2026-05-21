import json
import sys
from pathlib import Path

import nonebot

nonebot.init(log_level="INFO")

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mytest.plugins.dice_girl_plugin.llm.prompts import (  # noqa: E402
    build_chat_prompt,
    build_dice_prompt,
    build_dice_user_prompt,
)
from mytest.plugins.dice_girl_plugin.llm.response import (  # noqa: E402
    parse_chat_reply,
    parse_dice_reaction,
)
from mytest.plugins.dice_girl_plugin.persona import persona_manager  # noqa: E402


CASES_PATH = Path(__file__).with_name("cases.json")


def _load_cases() -> dict:
    with CASES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _role(role_id: str) -> dict:
    return persona_manager.data["roles"][role_id]


def _assert_terms(prompt: str, terms: list[str], case_name: str) -> None:
    missing = [term for term in terms if term not in prompt]
    if missing:
        raise AssertionError(f"{case_name}: prompt missing terms: {missing}")


def _dump_response(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _assert_reply_terms(reply: str, terms: list[str], case_name: str) -> None:
    missing = [term for term in terms if term not in reply]
    if missing:
        raise AssertionError(f"{case_name}: reply missing terms: {missing}")


def run() -> None:
    cases = _load_cases()

    for case in cases.get("chat", []):
        prompt = build_chat_prompt(
            _role(case["role"]),
            case["favorability"],
            case["user_name"],
            case["user_text"],
            memory_summary="用户过去提到过喜欢安静陪伴。",
        )
        _assert_terms(prompt, case["expected_prompt_terms"], case["name"])
        parsed = parse_chat_reply(_dump_response(case["sample_response"]))
        if not parsed["reply"] or not -3 <= parsed["delta"] <= 3:
            raise AssertionError(f"{case['name']}: invalid parsed chat response")
        _assert_reply_terms(
            parsed["reply"],
            case.get("expected_reply_terms", []),
            case["name"],
        )
        if "expected_delta" in case and parsed["delta"] != case["expected_delta"]:
            raise AssertionError(
                f"{case['name']}: expected delta {case['expected_delta']}, "
                f"got {parsed['delta']}"
            )

    for case in cases.get("dice", []):
        prompt = build_dice_prompt(
            _role(case["role"]),
            case["favorability"],
            case["user_name"],
            case["roll"]["event"],
        )
        user_prompt = build_dice_user_prompt(case["favorability"], case["roll"])
        _assert_terms(prompt, case["expected_prompt_terms"], case["name"])
        if case["roll"]["event"] not in user_prompt:
            raise AssertionError(f"{case['name']}: dice user prompt missing event")
        parsed = parse_dice_reaction(_dump_response(case["sample_response"]))
        if not parsed["reply"] or parsed["delta"] != 0:
            raise AssertionError(f"{case['name']}: invalid parsed dice response")
        _assert_reply_terms(
            parsed["reply"],
            case.get("expected_reply_terms", []),
            case["name"],
        )

    print("dice girl local evals passed")


if __name__ == "__main__":
    run()
