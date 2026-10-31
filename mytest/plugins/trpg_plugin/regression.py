"""回归用例：给模型一个固定的局面，检查它的输出是不是符合规则。不依赖 NoneBot。

用例在 regression_cases.json，每个用例：
- setup：局面（场景、已设置的标记、已揭示的线索、在场调查员、最近的检定）
- stage：intent / arbiter / narrator / oog
- input：这一环节的输入（玩家原话、整理好的行动、本轮结果、场外问题）
- expect：对输出的断言
- fake：假模型的回复（pytest 用；没有就只能用真模型跑）

pytest 用 fake 回复跑一遍，确认用例本身和断言逻辑没问题；
真模型跑：python scripts/trpg_regression.py（在服务器上，用 WebUI 里配的模型），
看提示词在真模型上表现怎么样。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ai import AllFailed, Endpoint, ModelClient, Transport
from .campaign import Actor, Campaigns, ModuleLibrary
from .narrate import check_narration, narration_words, narrator_messages
from .resolve import Outcome, Resolution, arbiter_request, intent_request, situation
from .store import Store

CASES = Path(__file__).resolve().parent / "regression_cases.json"
GROUP = "regression"


@dataclass
class CaseResult:
    name: str
    ok: bool
    failures: list[str] = field(default_factory=list)
    output: Any = None


def load_cases(path: Path = CASES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _build(tmp: Path, setup: dict) -> tuple[Campaigns, int, Store]:
    store = Store(tmp / "regression.db")
    campaigns = Campaigns(store, ModuleLibrary(store), lambda: 1_700_000_000)
    campaigns.open(GROUP, setup.get("module", "茶馆失物"), Actor("1"))
    for user, pregen in setup.get("players", {"1001": "林晓", "1002": "赵川"}).items():
        campaigns.join(GROUP, Actor(user), pregen=pregen)
    c, _ = campaigns.start(GROUP, Actor("1"))
    with store.transaction() as db:
        if setup.get("scene"):
            store.update_campaign(db, c.id, 0, scene=setup["scene"])
        for f in setup.get("flags", []):
            store.set_flag(db, c.id, f, 1)
        for clue in setup.get("revealed", []):
            store.reveal_clue(db, c.id, clue, "1001", 1)
        for e in setup.get("fired", []):
            store.fire_event(db, c.id, e, 1)
        for r in setup.get("recent_rolls", []):
            store.insert_roll(db, c.id, 1, r["user"], "check", skill=r["skill"], value=50, difficulty="regular",
                              bonus=0, roll=80, target=50, level="失败", passed=False, object=r["object"], now=0)
        if setup.get("round"):
            store.update_campaign(db, c.id, 0, round=setup["round"])
    return campaigns, c.id, store


def _outcome(d: dict) -> Outcome:
    return Outcome(**{k: v for k, v in d.items() if k in Outcome.__dataclass_fields__ and k != "check"})


async def run_case(case: dict, client: ModelClient, endpoints: list[Endpoint]) -> CaseResult:
    with tempfile.TemporaryDirectory() as tmp:
        campaigns, cid, store = _build(Path(tmp), case.get("setup", {}))
        try:
            return await _run(case, campaigns, cid, client, endpoints)
        finally:
            store.close()


async def _run(case: dict, campaigns: Campaigns, cid: int, client: ModelClient,
               endpoints: list[Endpoint]) -> CaseResult:
    stage, inp, expect = case["stage"], case["input"], case.get("expect", {})
    result = CaseResult(case["name"], True)

    def fail(msg: str) -> None:
        result.ok = False
        result.failures.append(msg)

    try:
        if stage == "intent":
            s = situation(campaigns, cid, inp["actions"], ())
            messages, schema, validate = intent_request(s)
            out = (await client.json(endpoints, messages, schema=schema, name="intent", validate=validate)).value
            result.output = out
            _check_actions(out["actions"], expect, fail)
        elif stage == "arbiter":
            s = situation(campaigns, cid, {a["user"]: a["summary"] for a in inp["intent"]["actions"]}, ())
            messages, schema, validate = arbiter_request(s, inp["intent"])
            out = (await client.json(endpoints, messages, schema=schema, name="arbiter", validate=validate)).value
            result.output = out
            _check_rulings(out["rulings"], expect, fail)
        elif stage == "narrator":
            s = situation(campaigns, cid, {}, ())
            res = Resolution(1, [_outcome(o) for o in inp["outcomes"]], [], s.campaign.scene,
                             inp.get("scene_after", s.campaign.scene), 0, inp.get("ending", ""))
            revealed = s.revealed | {c for o in res.outcomes for c in o.clues}
            words = narration_words(s, res, revealed, campaigns.store.fired_events(cid), s.told_npcs)
            messages = narrator_messages(s, res, [], "", 120, 260)
            text = (await client.text(endpoints, messages)).value
            result.output = text
            for p in check_narration(text, s, res, words, 260):
                fail(f"叙述检查没通过：{p}")
            for word in expect.get("contains", []):
                if word not in text:
                    fail(f"叙述里应该有「{word}」")
            for word in expect.get("not_contains", []):
                if word in text:
                    fail(f"叙述里不应该有「{word}」")
        elif stage == "oog":
            from .play import Engine
            from .rounds import Rounds

            eng = Engine(campaigns, Rounds(campaigns), client, lambda role: endpoints)
            messages = eng.oog_messages(cid, inp.get("user", "1001"), inp["question"])
            text = (await client.text(endpoints, messages, max_tokens=600, temperature=0.3)).value
            result.output = text
            for word in expect.get("not_contains", []):
                if word in text:
                    fail(f"回答里不应该有「{word}」")
            any_of = expect.get("contains_any", [])
            if any_of and not any(w in text for w in any_of):
                fail(f"回答里应该提到其中之一：{'、'.join(any_of)}")
        else:
            fail(f"不认识的环节 {stage}")
    except AllFailed as e:
        fail(f"模型没有给出合格的输出：{e}")
    return result


def _check_actions(actions: list[dict], expect: dict, fail: Callable[[str], None]) -> None:
    by_user = {a["user"]: a for a in actions}
    for user, rules in expect.get("actions", {}).items():
        a = by_user.get(user)
        if a is None:
            fail(f"{user} 没有意图")
            continue
        _check_fields(a, rules, f"{user} 的意图", fail)


def _check_rulings(rulings: list[dict], expect: dict, fail: Callable[[str], None]) -> None:
    by_user = {r["user"]: r for r in rulings}
    for user, rules in expect.get("rulings", {}).items():
        r = by_user.get(user)
        if r is None:
            fail(f"{user} 没有裁决")
            continue
        _check_fields(r, rules, f"{user} 的裁决", fail)


def _get(d: dict, path: str) -> Any:
    for part in path.split("."):
        d = d.get(part) if isinstance(d, dict) else None
        if d is None:
            return None
    return d


def _check_fields(value: dict, rules: dict, where: str, fail: Callable[[str], None]) -> None:
    """rules：{"字段": 值}（相等）、{"字段__in": [...]}（之一）、{"字段__nonempty": true}、
    {"字段__has": "x"}（列表包含 / 文字包含）、{"字段__lacks": "x"}、{"字段__empty": true}。字段可以用 a.b 取下一层。"""
    for key, want in rules.items():
        name, _, op = key.partition("__")
        got = _get(value, name)
        if op == "":
            ok = got == want or (got is None and want in ("", False, []))
        elif op == "in":
            ok = got in want
        elif op == "nonempty":
            ok = bool(got) == bool(want)
        elif op == "empty":
            ok = (not got) == bool(want)
        elif op == "has":
            ok = got is not None and want in got
        elif op == "lacks":
            ok = got is None or want not in got
        else:
            fail(f"用例写错了：不认识 {key}")
            continue
        if not ok:
            fail(f"{where}：{key} 应该是 {want!r}，实际是 {got!r}")


# ---------------------------------------------------------------- 假模型


def fake_transport(case: dict) -> Transport:
    replies = list(case.get("fake", []))

    async def send(endpoint, payload):
        from .ai import Reply

        if not replies:
            raise AssertionError(f"用例「{case['name']}」的 fake 回复不够")
        item = replies.pop(0)
        return Reply(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))

    return send


async def _no_sleep(_: float) -> None:
    return None


async def run_fake(case: dict) -> CaseResult:
    client = ModelClient(fake_transport(case), sleep=_no_sleep)
    return await run_case(case, client, [Endpoint("假模型", "https://fake/v1", "k", "fake")])


# ---------------------------------------------------------------- 真模型（命令行）


async def run_all(client: ModelClient, endpoints: list[Endpoint], names: list[str] | None = None,
                  report: Callable[[CaseResult], Awaitable[None] | None] | None = None) -> list[CaseResult]:
    results = []
    for case in load_cases():
        if names and case["name"] not in names:
            continue
        r = await run_case(case, client, endpoints)
        results.append(r)
        if report is not None:
            maybe = report(r)
            if asyncio.iscoroutine(maybe):
                await maybe
    return results


def main() -> None:  # pragma: no cover - 在服务器上手动跑，入口是 scripts/trpg_regression.py
    from .config import endpoints, output_mode

    eps = endpoints("arbiter")
    if not eps:
        print("没有可用的模型：先在 WebUI「大模型」里填接口地址和密钥")
        sys.exit(1)
    client = ModelClient(mode=output_mode)
    print(f"用 {eps[0].label} {eps[0].model} 跑回归用例（共 {len(load_cases())} 个）\n")

    def show(r: CaseResult) -> None:
        print(("✓ " if r.ok else "✗ ") + r.name)
        for f in r.failures:
            print("    " + f)
        if not r.ok and r.output is not None:
            text = r.output if isinstance(r.output, str) else json.dumps(r.output, ensure_ascii=False)
            print("    输出：" + text[:400])

    results = asyncio.run(run_all(client, eps, sys.argv[1:] or None, show))
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} 通过")
    sys.exit(0 if passed == len(results) else 1)


__all__ = ["CaseResult", "load_cases", "run_all", "run_case", "run_fake"]
