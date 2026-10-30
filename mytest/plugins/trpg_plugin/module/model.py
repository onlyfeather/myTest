"""模组的数据结构。只由 loader 构造，构造前已经校验过，这里不再检查。不依赖 NoneBot。

可见性：
- 给玩家看（叙述环节能看到）：intro、场景 name/public、出口 how、NPC name/public、
  已揭示线索的 text、已发生事件的 public、结局的 public。
- 只给裁决环节：truth、场景 hidden、NPC personality/knows/secrets/lies、
  未揭示的线索、flags、hints。
"""

from __future__ import annotations

from dataclasses import dataclass, field

FORMAT_VERSION = 1

REGULAR = "regular"
HARD = "hard"
EXTREME = "extreme"
DIFFICULTIES = (REGULAR, HARD, EXTREME)

ATTITUDES = ("friendly", "neutral", "wary", "hostile")


@dataclass(frozen=True)
class Condition:
    """几个条件同时满足才成立；空条件总是成立。"""

    flags_all: tuple[str, ...] = ()
    flags_any: tuple[str, ...] = ()
    clues_all: tuple[str, ...] = ()
    scene: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.flags_all or self.flags_any or self.clues_all or self.scene)

    def met(self, flags: set[str], clues: set[str], scene: str) -> bool:
        return (
            all(f in flags for f in self.flags_all)
            and (not self.flags_any or any(f in flags for f in self.flags_any))
            and all(c in clues for c in self.clues_all)
            and (self.scene is None or self.scene == scene)
        )


@dataclass(frozen=True)
class Exit:
    to: str
    how: str
    requires: Condition = field(default_factory=Condition)


@dataclass(frozen=True)
class Scene:
    id: str
    name: str
    public: str
    hidden: str = ""
    exits: tuple[Exit, ...] = ()
    npcs: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckSpec:
    skill: str
    difficulty: str = REGULAR


@dataclass(frozen=True)
class Clue:
    id: str
    scene: str
    where: str
    text: str
    how: CheckSpec | None = None  # None：查对了地方直接给
    key: bool = False
    on_fail: str = ""
    set_flags: tuple[str, ...] = ()
    spoiler_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class Npc:
    id: str
    name: str
    public: str
    personality: str = ""
    knows: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    lies: tuple[str, ...] = ()
    attitude: str = "neutral"
    social: dict[str, int] = field(default_factory=dict)
    spoiler_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class SanityLoss:
    success: str
    fail: str


@dataclass(frozen=True)
class Event:
    id: str
    when: Condition
    public: str
    once: bool = True
    sanity: SanityLoss | None = None
    set_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Flag:
    id: str
    desc: str


@dataclass(frozen=True)
class Ending:
    id: str
    when: Condition
    public: str


@dataclass(frozen=True)
class Pregen:
    name: str
    occupation: str
    sheet: dict[str, int]
    background: str = ""


@dataclass(frozen=True)
class Module:
    id: str
    title: str
    tone: str
    era: str
    players: tuple[int, int]
    intro: str
    truth: str
    start_scene: str
    scenes: tuple[Scene, ...]
    clues: tuple[Clue, ...] = ()
    npcs: tuple[Npc, ...] = ()
    events: tuple[Event, ...] = ()
    flags: tuple[Flag, ...] = ()
    endings: tuple[Ending, ...] = ()
    pregens: tuple[Pregen, ...] = ()
    spoiler_words: tuple[str, ...] = ()  # 真相的关键词

    def scene(self, scene_id: str) -> Scene | None:
        return next((s for s in self.scenes if s.id == scene_id), None)

    def clue(self, clue_id: str) -> Clue | None:
        return next((c for c in self.clues if c.id == clue_id), None)

    def npc(self, npc_id: str) -> Npc | None:
        return next((n for n in self.npcs if n.id == npc_id), None)

    def clues_in(self, scene_id: str) -> tuple[Clue, ...]:
        return tuple(c for c in self.clues if c.scene == scene_id)

    def all_spoiler_words(self) -> set[str]:
        words = set(self.spoiler_words)
        for item in (*self.clues, *self.npcs):
            words.update(item.spoiler_words)
        return words
