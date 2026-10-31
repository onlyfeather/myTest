"""跑团测试共用的假模型、固定骰子、时钟和几个小工具。不是测试文件（不以 test_ 开头）。

用法：测试文件里 `from .trpg_helpers import make_game`，再写一个 game fixture 调用它。
"""

import asyncio
import json
import random

from mytest.plugins.trpg_plugin.ai import Endpoint, ModelClient, Reply
from mytest.plugins.trpg_plugin.campaign import Actor, Campaigns, ModuleLibrary
from mytest.plugins.trpg_plugin.commands import Caller, run
from mytest.plugins.trpg_plugin.play import Engine
from mytest.plugins.trpg_plugin.rounds import Rounds, Timing
from mytest.plugins.trpg_plugin.store import Store

GROUP = "1130489499"
LIN, ZHAO, SU = "1001", "1002", "1003"
T = Timing(ask_after=30 * 60, standby_after=12 * 3600, settle_delay=60)
EP = [Endpoint("主模型", "https://a/v1", "k", "m")]


class Fixed(random.Random):
    """randint 依次返回准备好的值；d100(…) 按「先个位再十位」排好一次 D100。"""

    def __init__(self):
        super().__init__(0)
        self.values: list[int] = []

    def d100(self, *values):
        for v in values:
            v %= 100
            self.values += [v % 10, v // 10]

    def randint(self, a, b):
        value = self.values.pop(0)
        assert a <= value <= b, (a, b, value)
        return value


class Brain:
    """假模型：按环节分别排队。环节由 system 提示词的开头认出来。"""

    STAGES = {"你负责理解": "intent", "你是 CoC 7 版的裁定员。你读到": "arbiter", "你是一场": "narrator",
              "你是 CoC 7 版的裁定员。一名": "push", "你是跑团主持人": "oog", "你是跑团的记录员": "summary",
              "你在审一段": "review"}

    def __init__(self):
        self.queues: dict[str, list] = {k: [] for k in self.STAGES.values()}
        self.calls: list[tuple[str, dict]] = []

    def add(self, stage, *items):
        self.queues[stage] += items
        return self

    def inputs(self, stage):
        """这个环节每次收到的 user 内容。"""
        return [p["messages"][1]["content"] for st, p in self.calls if st == stage]

    async def __call__(self, endpoint, payload):
        system = payload["messages"][0]["content"]
        stage = next(v for k, v in self.STAGES.items() if system.startswith(k))
        self.calls.append((stage, payload))
        if not self.queues[stage]:
            raise AssertionError(f"没有给 {stage} 准备回复")
        item = self.queues[stage].pop(0)
        if isinstance(item, Exception):
            raise item
        return Reply(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))


async def no_sleep(_):
    return None


class Clock:
    def __init__(self):
        self.now = 1_700_000_000

    def __call__(self):
        return self.now


def make_game(tmp_path):
    """返回 (campaigns, rounds, engine, brain, rng, clock, store)。"""
    store = Store(tmp_path / "trpg.db")
    clock = Clock()
    campaigns = Campaigns(store, ModuleLibrary(store), clock)
    rounds = Rounds(campaigns)
    brain = Brain()
    rng = Fixed()
    engine = Engine(campaigns, rounds, ModelClient(brain, sleep=no_sleep), lambda role: EP, rng, clock)
    return (campaigns, rounds, engine, brain, rng, clock), store


def cmd(campaigns, command, arg="", user=LIN, manager=False, name=""):
    return run(campaigns, command, arg, Caller(GROUP, Actor(user, manager), name, dict))


def started(game, *pregens):
    campaigns = game[0]
    cmd(campaigns, "开团", "茶馆失物")
    for user, name in pregens or ((LIN, "林晓"), (ZHAO, "赵川")):
        cmd(campaigns, "加入", name, user)
    return cmd(campaigns, "开始")


def act(game, user, text, mid):
    campaigns, rounds, _, _, _, clock = game
    clock.now += 1
    return rounds.receive(GROUP, user, str(mid), text, clock.now)


def collect(game):
    """等够时间，收齐这一轮。"""
    campaigns, rounds, _, _, _, clock = game
    clock.now += 61
    rounds.tick(clock.now, T)


def settle(game, engine=None):
    """等够时间、收齐、结算。返回发到群里的消息。"""
    collect(game)
    campaigns = game[0]
    c = campaigns.active(GROUP)
    return asyncio.run((engine or game[2]).settle(c.id)) if c else []


def intent(*actions):
    return {"actions": [{"user": u, "summary": s, "kind": k, **kw} for u, s, k, kw in actions]}


def ruling(user, needs_roll=False, **kw):
    return {"user": user, "needs_roll": needs_roll, **kw}
