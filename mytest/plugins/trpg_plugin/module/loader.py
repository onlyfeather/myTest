"""读入模组 JSON 并校验。不依赖 NoneBot（骰子和技能表用到骰娘的纯函数）。

模组是手写的 JSON，允许 // 和 /* */ 注释。出错时尽量一次把所有问题都找出来，
每个问题带上位置（例如 scenes[s1].exits[0].to），WebUI 和命令行直接显示。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .model import (
    ATTITUDES,
    DIFFICULTIES,
    EXTREME,
    FORMAT_VERSION,
    HARD,
    REGULAR,
    CheckSpec,
    Clue,
    Condition,
    Ending,
    Event,
    Exit,
    Flag,
    Module,
    Npc,
    Pregen,
    SanityLoss,
    Scene,
)

# 内部 id 用无含义的编号，避免 id 本身泄密
ID_PATTERNS = {
    "scene": (re.compile(r"^s\d{1,3}$"), "s1"),
    "clue": (re.compile(r"^c\d{1,3}$"), "c1"),
    "npc": (re.compile(r"^n\d{1,3}$"), "n1"),
    "event": (re.compile(r"^e\d{1,3}$"), "e1"),
    "flag": (re.compile(r"^f\d{1,3}$"), "f1"),
    "ending": (re.compile(r"^end\d{1,3}$"), "end1"),
}
MODULE_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

MAX_SHORT = 40  # 名字、地点
MAX_TEXT = 1500  # 一段描述
MAX_LONG = 4000  # 开场、真相
MAX_ITEMS = 60  # 每种东西最多几个
MAX_LIST = 20  # 一个列表字段最多几项
MAX_PLAYERS = 5
REQUIRED_SHEET = ("理智", "智力", "幸运")

DIFFICULTY_NAMES = {"常规": REGULAR, "普通": REGULAR, "困难": HARD, "极难": EXTREME}
ATTITUDE_NAMES = {"友好": "friendly", "中立": "neutral", "戒备": "wary", "敌对": "hostile"}


@dataclass(frozen=True)
class Problem:
    level: str  # "error" 或 "warning"
    where: str
    message: str

    def __str__(self) -> str:
        tag = "错误" if self.level == "error" else "提醒"
        return f"[{tag}] {self.where}：{self.message}" if self.where else f"[{tag}] {self.message}"


@dataclass
class LoadResult:
    module: Module | None
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "warning"]

    @property
    def ok(self) -> bool:
        return self.module is not None and not self.errors

    def report(self) -> str:
        if not self.problems:
            return "校验通过"
        head = f"{len(self.errors)} 个错误，{len(self.warnings)} 个提醒"
        return "\n".join([head, *map(str, self.problems)])


# ---------------------------------------------------------------- JSON


def strip_comments(text: str) -> str:
    """去掉 // 和 /* */ 注释。注释换成空格（换行保留），JSON 报错的行列号不变。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_module_text(text: str) -> LoadResult:
    try:
        data = json.loads(strip_comments(text.lstrip("\ufeff")))
    except json.JSONDecodeError as e:
        return LoadResult(None, [Problem("error", f"第 {e.lineno} 行第 {e.colno} 列", f"JSON 格式错误（{e.msg}）")])
    return load_module_data(data)


def load_module_data(data: Any) -> LoadResult:
    reader = _Reader()
    module = reader.module(data)
    if module is not None and not reader.errors:
        _validate(module, reader)
    return LoadResult(module if not reader.errors else None, reader.problems)


# ---------------------------------------------------------------- 逐项读取


class _Reader:
    def __init__(self) -> None:
        self.problems: list[Problem] = []

    @property
    def errors(self) -> bool:
        return any(p.level == "error" for p in self.problems)

    def error(self, where: str, message: str) -> None:
        self.problems.append(Problem("error", where, message))

    def warn(self, where: str, message: str) -> None:
        self.problems.append(Problem("warning", where, message))

    # ---- 基本类型

    def obj(self, value: Any, where: str, fields: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.error(where, "应该是一个对象 {…}")
            return {}
        for key in value:
            if key not in fields:
                close = difflib.get_close_matches(str(key), sorted(fields), n=1)
                hint = f"，是不是「{close[0]}」？" if close else ""
                self.error(f"{where}.{key}", f"不认识这个字段{hint}")
        return value

    def text(self, d: dict, key: str, where: str, *, required: bool = True, limit: int = MAX_TEXT) -> str:
        value = d.get(key)
        if value is None or value == "":
            if required:
                self.error(f"{where}.{key}", "不能为空")
            return ""
        if not isinstance(value, str):
            self.error(f"{where}.{key}", "应该是文字")
            return ""
        value = value.strip()
        if len(value) > limit:
            self.error(f"{where}.{key}", f"太长了（{len(value)} 字，最多 {limit}）")
        return value

    def texts(self, d: dict, key: str, where: str, *, limit: int = MAX_TEXT) -> tuple[str, ...]:
        value = d.get(key)
        if value is None:
            return ()
        if not isinstance(value, list):
            self.error(f"{where}.{key}", "应该是列表 […]")
            return ()
        if len(value) > MAX_LIST:
            self.error(f"{where}.{key}", f"最多 {MAX_LIST} 项")
        items = []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.error(f"{where}.{key}[{i}]", "应该是不为空的文字")
            elif len(item.strip()) > limit:
                self.error(f"{where}.{key}[{i}]", f"太长了（最多 {limit} 字）")
            else:
                items.append(item.strip())
        return tuple(items)

    def boolean(self, d: dict, key: str, where: str, default: bool) -> bool:
        value = d.get(key, default)
        if not isinstance(value, bool):
            self.error(f"{where}.{key}", "应该是 true 或 false")
            return default
        return value

    def ident(self, value: Any, where: str, kind: str) -> str:
        pattern, example = ID_PATTERNS[kind]
        if not isinstance(value, str) or not pattern.match(value):
            self.error(where, f"编号格式不对，应该像 {example}、{example[:-1]}2（用无含义的编号，免得编号本身剧透）")
            return ""
        return value

    def idents(self, d: dict, key: str, where: str, kind: str) -> tuple[str, ...]:
        value = d.get(key)
        if value is None:
            return ()
        if not isinstance(value, list):
            self.error(f"{where}.{key}", "应该是编号列表，例如 [\"f1\"]")
            return ()
        return tuple(i for i in (self.ident(v, f"{where}.{key}[{n}]", kind) for n, v in enumerate(value)) if i)

    def items(self, data: dict, key: str, *, required: bool = False) -> list[tuple[str, Any]]:
        """列表字段 → [(位置, 内容)]；位置用 id 标出，例如 scenes[s2]。"""
        value = data.get(key)
        if value is None or value == []:
            if required:
                self.error(key, "至少要有一项")
            return []
        if not isinstance(value, list):
            self.error(key, "应该是列表 […]")
            return []
        if len(value) > MAX_ITEMS:
            self.error(key, f"最多 {MAX_ITEMS} 项")
        out = []
        for i, item in enumerate(value):
            label = item.get("id") or item.get("name") if isinstance(item, dict) else None
            out.append((f"{key}[{label}]" if isinstance(label, str) and label else f"{key}[{i}]", item))
        return out

    # ---- 各部分

    def condition(self, value: Any, where: str) -> Condition:
        if value is None:
            return Condition()
        d = self.obj(value, where, {"flags_all", "flags_any", "clues_all", "scene"})
        scene = d.get("scene")
        return Condition(
            flags_all=self.idents(d, "flags_all", where, "flag"),
            flags_any=self.idents(d, "flags_any", where, "flag"),
            clues_all=self.idents(d, "clues_all", where, "clue"),
            scene=self.ident(scene, f"{where}.scene", "scene") or None if scene is not None else None,
        )

    def scene(self, value: Any, where: str) -> Scene:
        d = self.obj(value, where, {"id", "name", "public", "hidden", "exits", "npcs", "hints"})
        exits = []
        raw_exits = d.get("exits") or []
        if not isinstance(raw_exits, list):
            self.error(f"{where}.exits", "应该是列表 […]")
            raw_exits = []
        for i, raw in enumerate(raw_exits):
            w = f"{where}.exits[{i}]"
            e = self.obj(raw, w, {"to", "how", "requires"})
            exits.append(Exit(
                to=self.ident(e.get("to"), f"{w}.to", "scene"),
                how=self.text(e, "how", w, limit=MAX_SHORT * 3),
                requires=self.condition(e.get("requires"), f"{w}.requires"),
            ))
        return Scene(
            id=self.ident(d.get("id"), f"{where}.id", "scene"),
            name=self.text(d, "name", where, limit=MAX_SHORT),
            public=self.text(d, "public", where),
            hidden=self.text(d, "hidden", where, required=False),
            exits=tuple(exits),
            npcs=self.idents(d, "npcs", where, "npc"),
            hints=self.texts(d, "hints", where, limit=300),
        )

    def check_spec(self, value: Any, where: str) -> CheckSpec | None:
        if value is None:
            return None
        d = self.obj(value, where, {"skill", "difficulty"})
        from ..rules.skills import canonical, is_known

        skill = self.text(d, "skill", where, limit=MAX_SHORT)
        if skill and not is_known(skill):
            self.error(f"{where}.skill", f"「{skill}」不是 CoC 7 版的技能或属性")
        return CheckSpec(canonical(skill) if skill else "", self.difficulty(d.get("difficulty"), f"{where}.difficulty"))

    def difficulty(self, value: Any, where: str) -> str:
        if value is None:
            return REGULAR
        value = DIFFICULTY_NAMES.get(value, value) if isinstance(value, str) else value
        if value not in DIFFICULTIES:
            self.error(where, "难度只能是 regular / hard / extreme（或 常规 / 困难 / 极难）")
            return REGULAR
        return value

    def clue(self, value: Any, where: str) -> Clue:
        d = self.obj(value, where, {"id", "scene", "where", "text", "how", "key", "on_fail", "set_flags", "spoiler_words"})
        return Clue(
            id=self.ident(d.get("id"), f"{where}.id", "clue"),
            scene=self.ident(d.get("scene"), f"{where}.scene", "scene"),
            where=self.text(d, "where", where, limit=MAX_SHORT),
            text=self.text(d, "text", where),
            how=self.check_spec(d.get("how"), f"{where}.how"),
            key=self.boolean(d, "key", where, False),
            on_fail=self.text(d, "on_fail", where, required=False),
            set_flags=self.idents(d, "set_flags", where, "flag"),
            spoiler_words=self.texts(d, "spoiler_words", where, limit=MAX_SHORT),
        )

    def npc(self, value: Any, where: str) -> Npc:
        d = self.obj(value, where, {
            "id", "name", "public", "personality", "knows", "secrets", "lies", "attitude", "social", "spoiler_words",
        })
        attitude = d.get("attitude", "neutral")
        attitude = ATTITUDE_NAMES.get(attitude, attitude) if isinstance(attitude, str) else attitude
        if attitude not in ATTITUDES:
            self.error(f"{where}.attitude", "态度只能是 friendly / neutral / wary / hostile（或 友好 / 中立 / 戒备 / 敌对）")
            attitude = "neutral"
        return Npc(
            id=self.ident(d.get("id"), f"{where}.id", "npc"),
            name=self.text(d, "name", where, limit=MAX_SHORT),
            public=self.text(d, "public", where),
            personality=self.text(d, "personality", where, required=False),
            knows=self.texts(d, "knows", where),
            secrets=self.texts(d, "secrets", where),
            lies=self.texts(d, "lies", where),
            attitude=attitude,
            social=self.social(d.get("social"), f"{where}.social"),
            spoiler_words=self.texts(d, "spoiler_words", where, limit=MAX_SHORT),
        )

    def social(self, value: Any, where: str) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.error(where, "应该是对象，例如 {\"说服\": 50}")
            return {}
        from ..rules.skills import canonical, is_known

        out = {}
        for name, number in value.items():
            if not is_known(name):
                self.error(f"{where}.{name}", f"「{name}」不是 CoC 7 版的技能")
            elif not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 100:
                self.error(f"{where}.{name}", "应该是 1 到 100 的整数")
            else:
                out[canonical(name)] = number
        return out

    def sanity(self, value: Any, where: str) -> SanityLoss | None:
        if value is None:
            return None
        d = self.obj(value, where, {"success", "fail"})
        from ...dice_girl_plugin.dice.expression import DiceError, roll

        parts = []
        for key in ("success", "fail"):
            raw = d.get(key)
            expr = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
            try:
                roll(expr, require_dice=False)
            except DiceError:
                self.error(f"{where}.{key}", "应该是骰子表达式，例如 \"0\"、\"1\"、\"1d3\"")
                expr = "0"
            parts.append(expr)
        return SanityLoss(*parts)

    def event(self, value: Any, where: str) -> Event:
        d = self.obj(value, where, {"id", "when", "public", "once", "sanity", "set_flags"})
        when = self.condition(d.get("when"), f"{where}.when")
        if when.empty:
            self.error(f"{where}.when", "要写触发条件（开场就发生的内容写进 intro）")
        return Event(
            id=self.ident(d.get("id"), f"{where}.id", "event"),
            when=when,
            public=self.text(d, "public", where),
            once=self.boolean(d, "once", where, True),
            sanity=self.sanity(d.get("sanity"), f"{where}.sanity"),
            set_flags=self.idents(d, "set_flags", where, "flag"),
        )

    def flag(self, value: Any, where: str) -> Flag:
        d = self.obj(value, where, {"id", "desc"})
        return Flag(self.ident(d.get("id"), f"{where}.id", "flag"), self.text(d, "desc", where, limit=200))

    def ending(self, value: Any, where: str) -> Ending:
        d = self.obj(value, where, {"id", "when", "public"})
        when = self.condition(d.get("when"), f"{where}.when")
        if when.empty:
            self.error(f"{where}.when", "要写达成条件")
        return Ending(self.ident(d.get("id"), f"{where}.id", "ending"), when, self.text(d, "public", where))

    def pregen(self, value: Any, where: str) -> Pregen:
        d = self.obj(value, where, {"name", "occupation", "sheet", "background"})
        return Pregen(
            name=self.text(d, "name", where, limit=MAX_SHORT),
            occupation=self.text(d, "occupation", where, limit=MAX_SHORT),
            sheet=self.sheet(d.get("sheet"), f"{where}.sheet"),
            background=self.text(d, "background", where, required=False),
        )

    def sheet(self, value: Any, where: str) -> dict[str, int]:
        """角色卡：「侦查60 图书馆70 理智55」，或 {"侦查": 60, …}。"""
        from ...dice_girl_plugin.dice.coc import DiceError, parse_st
        from ..rules.skills import canonical, is_known

        if isinstance(value, str):
            try:
                items = parse_st(value)
            except DiceError as e:
                self.error(where, str(e))
                return {}
            pairs: list[tuple[str, Any]] = []
            for name, op, number in items:
                if op:
                    self.error(where, f"「{name}{op}{number}」：预设卡里不能写加减")
                pairs.append((name, number))
        elif isinstance(value, dict):
            pairs = list(value.items())
        else:
            self.error(where, "应该是 \"侦查60 图书馆70 理智55\" 这样的文字，或 {\"侦查\": 60}")
            return {}
        sheet: dict[str, int] = {}
        for name, number in pairs:
            name = canonical(str(name))
            if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 99:
                self.error(f"{where}.{name}", "应该是 0 到 99 的整数")
                continue
            if not is_known(name):
                self.warn(f"{where}.{name}", "不是 CoC 7 版的标准技能，检定时照常使用")
            sheet[name] = number
        missing = [n for n in REQUIRED_SHEET if n not in sheet]
        if missing and sheet:
            self.error(where, f"缺少 {'、'.join(missing)}（理智检定、灵感检定、花幸运都要用）")
        return sheet

    def module(self, data: Any) -> Module | None:
        d = self.obj(data, "", {
            "format", "id", "title", "tone", "era", "players", "intro", "truth", "spoiler_words",
            "start_scene", "scenes", "clues", "npcs", "events", "flags", "endings", "pregens",
        })
        if not d:
            return None
        if d.get("format") != FORMAT_VERSION:
            self.error("format", f"应该是 {FORMAT_VERSION}")
        module_id = d.get("id")
        if not isinstance(module_id, str) or not MODULE_ID.match(module_id):
            self.error("id", "小写字母开头，只用小写字母、数字、下划线，2 到 32 个字符，例如 m_tea_house")
        players = d.get("players", [1, MAX_PLAYERS])
        if (not isinstance(players, list) or len(players) != 2
                or not all(isinstance(p, int) and not isinstance(p, bool) for p in players)
                or not 1 <= players[0] <= players[1] <= MAX_PLAYERS):
            self.error("players", f"应该是 [最少人数, 最多人数]，在 1 到 {MAX_PLAYERS} 之间")
            players = [1, MAX_PLAYERS]
        return Module(
            id=module_id if isinstance(module_id, str) else "",
            title=self.text(d, "title", "", limit=MAX_SHORT),
            tone=self.text(d, "tone", "", limit=MAX_SHORT),
            era=self.text(d, "era", "", limit=MAX_SHORT * 2),
            players=(players[0], players[1]),
            intro=self.text(d, "intro", "", limit=MAX_LONG),
            truth=self.text(d, "truth", "", limit=MAX_LONG),
            spoiler_words=self.texts(d, "spoiler_words", "", limit=MAX_SHORT),
            start_scene=self.ident(d.get("start_scene"), "start_scene", "scene"),
            scenes=tuple(self.scene(v, w) for w, v in self.items(d, "scenes", required=True)),
            clues=tuple(self.clue(v, w) for w, v in self.items(d, "clues")),
            npcs=tuple(self.npc(v, w) for w, v in self.items(d, "npcs")),
            events=tuple(self.event(v, w) for w, v in self.items(d, "events")),
            flags=tuple(self.flag(v, w) for w, v in self.items(d, "flags")),
            endings=tuple(self.ending(v, w) for w, v in self.items(d, "endings", required=True)),
            pregens=tuple(self.pregen(v, w) for w, v in self.items(d, "pregens")),
        )


# ---------------------------------------------------------------- 整体校验（各项都读对了才做）


def _validate(m: Module, r: _Reader) -> None:
    _check_unique(m, r)
    if r.errors:
        return
    _check_references(m, r)
    if r.errors:
        return
    _check_reachability(m, r)
    _check_spoilers(m, r)


def _check_unique(m: Module, r: _Reader) -> None:
    seen: dict[str, str] = {}
    groups = [("scenes", m.scenes), ("clues", m.clues), ("npcs", m.npcs),
              ("events", m.events), ("flags", m.flags), ("endings", m.endings)]
    for key, items in groups:
        for item in items:
            if item.id in seen:
                r.error(f"{key}[{item.id}]", "编号重复了")
            seen[item.id] = key
    names = [p.name for p in m.pregens]
    for name in {n for n in names if names.count(n) > 1}:
        r.error(f"pregens[{name}]", "预设调查员重名了")


def _check_references(m: Module, r: _Reader) -> None:
    scenes = {s.id for s in m.scenes}
    clues = {c.id for c in m.clues}
    npcs = {n.id for n in m.npcs}
    flags = {f.id for f in m.flags}

    def need(ids: tuple[str, ...] | list[str], pool: set[str], where: str, what: str) -> None:
        for i in ids:
            if i not in pool:
                r.error(where, f"{what} {i} 不存在")

    def cond(c: Condition, where: str) -> None:
        need(c.flags_all + c.flags_any, flags, where, "标记")
        need(c.clues_all, clues, where, "线索")
        if c.scene:
            need([c.scene], scenes, where, "场景")

    need([m.start_scene], scenes, "start_scene", "场景")
    for s in m.scenes:
        need(s.npcs, npcs, f"scenes[{s.id}].npcs", "NPC")
        for i, e in enumerate(s.exits):
            need([e.to], scenes, f"scenes[{s.id}].exits[{i}].to", "场景")
            if e.to == s.id:
                r.error(f"scenes[{s.id}].exits[{i}].to", "出口不能通向自己")
            cond(e.requires, f"scenes[{s.id}].exits[{i}].requires")
    for c in m.clues:
        need([c.scene], scenes, f"clues[{c.id}].scene", "场景")
        need(c.set_flags, flags, f"clues[{c.id}].set_flags", "标记")
    for e in m.events:
        cond(e.when, f"events[{e.id}].when")
        need(e.set_flags, flags, f"events[{e.id}].set_flags", "标记")
    for end in m.endings:
        cond(end.when, f"endings[{end.id}].when")


def _check_reachability(m: Module, r: _Reader) -> None:
    """不模拟整局，只查结构：场景能不能走到、条件用到的线索和标记有没有办法拿到。"""
    # 场景：从开场沿出口走（先不管出口条件，条件单独查）
    reachable = {m.start_scene}
    frontier = [m.start_scene]
    while frontier:
        scene = m.scene(frontier.pop())
        for e in scene.exits if scene else ():
            if e.to not in reachable:
                reachable.add(e.to)
                frontier.append(e.to)
    for s in m.scenes:
        if s.id not in reachable:
            if any(c.key for c in m.clues_in(s.id)):
                r.error(f"scenes[{s.id}]", "从开场走不到这个场景，但这里有关键线索")
            else:
                r.warn(f"scenes[{s.id}]", "从开场走不到这个场景")

    clue_ok = {c.id for c in m.clues if c.scene in reachable}

    # 标记不查来源：没有线索或事件设置的标记由裁决环节按 desc 判定后设置
    def check(c: Condition, where: str) -> bool:
        ok = True
        for cid in c.clues_all:
            if cid not in clue_ok:
                r.error(where, f"线索 {cid} 所在的场景走不到，这个条件永远达不成")
                ok = False
        if c.scene and c.scene not in reachable:
            r.error(where, f"场景 {c.scene} 走不到，这个条件永远达不成")
            ok = False
        return ok

    for s in m.scenes:
        for i, e in enumerate(s.exits):
            check(e.requires, f"scenes[{s.id}].exits[{i}].requires")
    for e in m.events:
        check(e.when, f"events[{e.id}].when")
    ending_ok = [end for end in m.endings if check(end.when, f"endings[{end.id}].when")]
    if not ending_ok:
        r.error("endings", "没有一个结局能达成")

    used: set[str] = set()
    conditions = [e.requires for s in m.scenes for e in s.exits]
    conditions += [e.when for e in m.events] + [e.when for e in m.endings]
    for c in conditions:
        used.update(c.flags_all + c.flags_any)
    for f in m.flags:
        if f.id not in used:
            r.warn(f"flags[{f.id}]", "没有出口、事件或结局用到这个标记")

    for c in m.clues:
        if c.key and c.how is not None and not c.on_fail:
            r.warn(f"clues[{c.id}]", "关键线索只能靠检定拿到，失败后没有别的办法。建议写 on_fail，或在别处再放一条")


def _check_spoilers(m: Module, r: _Reader) -> None:
    if m.truth and not m.spoiler_words:
        r.warn("spoiler_words", "真相没有写剧透关键词，叙述漏了真相时查不出来")
    for c in m.clues:
        if c.key and not c.spoiler_words:
            r.warn(f"clues[{c.id}]", "关键线索没有写 spoiler_words，提前漏出来时查不出来")
    for n in m.npcs:
        if n.secrets and not n.spoiler_words:
            r.warn(f"npcs[{n.id}]", "NPC 有秘密但没有写 spoiler_words，叙述漏了秘密时查不出来")

    owners: list[tuple[str, tuple[str, ...]]] = [("spoiler_words", m.spoiler_words)]
    owners += [(f"clues[{c.id}].spoiler_words", c.spoiler_words) for c in m.clues]
    owners += [(f"npcs[{n.id}].spoiler_words", n.spoiler_words) for n in m.npcs]
    for where, words in owners:
        for w in words:
            if len(w) < 2:
                r.error(where, f"「{w}」太短，会把正常的叙述也当成剧透")

    # 一开始就给玩家看的文字里不能有剧透关键词，否则每轮都会误判
    public = [("intro", m.intro)]
    for s in m.scenes:
        public += [(f"scenes[{s.id}].name", s.name), (f"scenes[{s.id}].public", s.public)]
        public += [(f"scenes[{s.id}].exits[{i}].how", e.how) for i, e in enumerate(s.exits)]
    for n in m.npcs:
        public += [(f"npcs[{n.id}].name", n.name), (f"npcs[{n.id}].public", n.public)]
    words = m.all_spoiler_words()
    for where, text in public:
        for w in sorted(words):
            if w in text:
                r.error(where, f"玩家一开始就能看到的文字里出现了剧透关键词「{w}」")
