"""酒馆（SillyTavern）预设：导入、保存、按会话拼成提示词。不依赖 NoneBot，方便单独测试。

只支持「对话补全」预设（带 prompts / prompt_order 的 JSON）。聊天记录、角色卡、世界书这类
占位条目（marker）由 MaiBot 自己负责，导入时跳过；剩下的条目按酒馆里的顺序和开关拼成一段文字，
加进凛音回复时的额外要求里。
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 300
MAX_NAME = 100
MAX_CONTENT = 20000
MAX_PROMPT_CHARS = 30000
ROLES = ("system", "user", "assistant")
# 酒馆里「当前预设」的顺序存在这个虚拟角色下，旧版本用 100000。
ORDER_IDS = ("100001", "100000")


@dataclass(frozen=True)
class Mode:
    id: str
    title: str
    description: str
    # 固定追加在预设后面，不能在预设里删掉。
    guard: str
    # 为 False 时原样输出条目内容，不加「【标题】」。
    header: bool = True


PREFIX = "prefix"

MODES: dict[str, Mode] = {
    m.id: m
    for m in (
        Mode(
            PREFIX,
            "前置",
            "NSFW 或强攻击力模式生效时，排在它们前面一起发送，原样输出、不加标题。单独不会生效。",
            "",
            header=False,
        ),
        Mode(
            "nsfw",
            "NSFW 模式",
            "成人向内容。只在白名单私聊和白名单群生效。",
            "所有出场人物都是成年人。任何时候都不描写未成年人的性内容，对方提到时直接拒绝。",
        ),
        Mode(
            "aggressive",
            "强攻击力模式",
            "直接开喷，可以骂人、爆粗口。私聊总是生效，群里只在白名单群生效。",
            "攻击只针对对方此刻的言行，不拿种族、地域、性别、性取向、残障等身份开刀，不劝人自残，不威胁现实伤害。",
        ),
    )
}


class PresetError(ValueError):
    """预设内容不合法。message 会原样显示给用户。"""


@dataclass
class Entry:
    id: str
    name: str
    role: str
    content: str
    enabled: bool


def new_id() -> str:
    return f"custom-{secrets.token_hex(6)}"


def clean_entries(raw: Any) -> list[Entry]:
    """校验条目列表（导入和 WebUI 修改都走这里）。id 缺失或重复时重新生成。"""
    if not isinstance(raw, list):
        raise PresetError("条目格式不正确")
    if len(raw) > MAX_ENTRIES:
        raise PresetError(f"条目太多了，最多 {MAX_ENTRIES} 条")
    entries: list[Entry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise PresetError(f"第 {index} 条格式不正确")
        name = str(item.get("name") or "").strip()[:MAX_NAME] or f"条目 {index}"
        role = str(item.get("role") or "system")
        if role not in ROLES:
            raise PresetError(f"「{name}」的角色只能是 system、user、assistant")
        content = item.get("content") or ""
        if not isinstance(content, str):
            raise PresetError(f"「{name}」的内容必须是文字")
        if len(content) > MAX_CONTENT:
            raise PresetError(f"「{name}」太长了，单条最多 {MAX_CONTENT} 字")
        entry_id = str(item.get("id") or "").strip()[:100]
        if not entry_id or entry_id in seen:
            entry_id = new_id()
        seen.add(entry_id)
        entries.append(Entry(entry_id, name, role, content, item.get("enabled", True) is True))
    return entries


def _pick_order(raw: Any) -> list[tuple[str, bool]]:
    if not isinstance(raw, list):
        return []
    candidates = [c for c in raw if isinstance(c, dict) and isinstance(c.get("order"), list)]
    if not candidates:
        return []
    by_id = {str(c.get("character_id")): c for c in candidates}
    chosen = next((by_id[i] for i in ORDER_IDS if i in by_id), None)
    chosen = chosen or max(candidates, key=lambda c: len(c["order"]))
    return [
        (str(item.get("identifier") or ""), item.get("enabled", True) is not False)
        for item in chosen["order"]
        if isinstance(item, dict)
    ]


def parse_st_preset(data: Any) -> tuple[list[Entry], int]:
    """把酒馆导出的预设转成条目列表，返回 (条目, 跳过的占位条目数)。"""
    if not isinstance(data, dict) or not isinstance(data.get("prompts"), list):
        raise PresetError("不是酒馆的对话补全预设：缺少 prompts")
    prompts: dict[str, dict[str, Any]] = {}
    skipped = 0
    for item in data["prompts"]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("identifier") or "").strip()
        if not ident or ident in prompts:
            continue
        content = item.get("content")
        if item.get("marker") or not isinstance(content, str) or not content.strip():
            skipped += 1
            continue
        prompts[ident] = item

    order = _pick_order(data.get("prompt_order"))
    raw: list[dict[str, Any]] = []
    placed: set[str] = set()

    def add(ident: str, enabled: bool) -> None:
        item = prompts[ident]
        placed.add(ident)
        role = item.get("role") if item.get("role") in ROLES else "system"
        raw.append({"id": ident, "name": item.get("name") or ident, "role": role,
                    "content": item["content"], "enabled": enabled})

    for ident, enabled in order:
        if ident in prompts and ident not in placed:
            add(ident, enabled)
    # 不在顺序里的条目酒馆不会发送，导入后默认关闭；整个文件没有顺序时按原样启用。
    for ident, item in prompts.items():
        if ident not in placed:
            add(ident, item.get("enabled", True) is not False if not order else False)
    if not raw:
        raise PresetError("预设里没有可用的条目")
    return clean_entries(raw), skipped


# ---------------------------------------------------------------- 拼提示词

_COMMENT = re.compile(r"\{\{//.*?\}\}", re.DOTALL)
_MACRO = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_TRIM = "\x00trim\x00"


def expand_macros(text: str, *, char: str, user: str, rng: random.Random | None = None) -> str:
    """支持 {{char}} {{user}} {{newline}} {{trim}} {{random}} 和 {{// 注释}}，其余宏直接去掉。"""
    rng = rng or random.Random()

    def replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        low = body.lower()
        if low == "char":
            return char
        if low == "user":
            return user
        if low == "newline":
            return "\n"
        if low == "trim":
            return _TRIM
        if low.startswith("random"):
            rest = body[6:]
            if rest.startswith("::"):
                options = rest[2:].split("::")
            elif rest.startswith(":"):
                options = rest[1:].split(",")
            else:
                return ""
            return rng.choice(options).strip()
        return ""

    text = _MACRO.sub(replace, _COMMENT.sub("", text))
    return re.sub(rf"\s*{_TRIM}\s*", "", text)


def render(mode: Mode, entries: list[Entry], *, char: str, user: str, rng: random.Random | None = None) -> str:
    """拼出一个模式的提示词。没有启用的条目时返回空字符串（模式不生效）。"""
    parts = [expand_macros(e.content, char=char, user=user, rng=rng).strip() for e in entries if e.enabled]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    body = "\n\n".join(parts)[:MAX_PROMPT_CHARS]
    text = f"【{mode.title}】\n{body}" if mode.header else body
    return f"{text}\n\n{mode.guard}" if mode.guard else text


def _ids(value: Any) -> set[str]:
    return {str(v).strip() for v in (value or []) if str(v).strip()}


def active_modes(cfg: Any, user_id: str, group_id: str | None) -> list[str]:
    """这个会话里开着哪些模式。cfg 只需要有 get(key)（配置分区视图或 dict）。"""
    modes = []
    if cfg.get("nsfw_enabled"):
        allowed = group_id in _ids(cfg.get("nsfw_groups")) if group_id else user_id in _ids(cfg.get("nsfw_users"))
        if allowed:
            modes.append("nsfw")
    if cfg.get("aggressive_enabled") and (not group_id or group_id in _ids(cfg.get("aggressive_groups"))):
        modes.append("aggressive")
    return modes


def build_prompt(
    store: PresetStore, cfg: Any, user_id: str, group_id: str | None, *,
    char: str, user: str, rng: random.Random | None = None,
) -> tuple[str, list[str]]:
    """返回 (要加进回复要求的文字, 实际生效的模式)。前置只在至少一个模式生效时加在最前面。"""
    parts, used = [], []
    for mode_id in active_modes(cfg, user_id, group_id):
        text = render(MODES[mode_id], store.entries(mode_id), char=char, user=user, rng=rng)
        if text:
            parts.append(text)
            used.append(mode_id)
    if used and cfg.get("prefix_enabled"):
        prefix = render(MODES[PREFIX], store.entries(PREFIX), char=char, user=user, rng=rng)
        if prefix:
            parts.insert(0, prefix)
            used.insert(0, PREFIX)
    return "\n\n".join(parts), used


# ---------------------------------------------------------------- 存储


def _mode(mode_id: str) -> Mode:
    mode = MODES.get(mode_id)
    if mode is None:
        raise KeyError(f"没有 {mode_id} 这个模式")
    return mode


class PresetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def entries(self, mode_id: str) -> list[Entry]:
        _mode(mode_id)
        with self._lock:
            return list(self._data.get(mode_id, {}).get("entries", []))

    def save(self, mode_id: str, name: str, entries: list[Entry]) -> None:
        _mode(mode_id)
        item = {"name": str(name or "").strip()[:MAX_NAME], "entries": list(entries), "updated_at": time.time()}
        with self._lock:
            data = {**self._data, mode_id: item}
            self._write(data)
            self._data = data

    def clear(self, mode_id: str) -> None:
        _mode(mode_id)
        with self._lock:
            data = {k: v for k, v in self._data.items() if k != mode_id}
            self._write(data)
            self._data = data

    def describe_mode(self, mode_id: str) -> dict[str, Any]:
        mode = _mode(mode_id)
        with self._lock:
            item = self._data.get(mode_id, {})
        return {
            "id": mode.id,
            "title": mode.title,
            "description": mode.description,
            "guard": mode.guard,
            "name": item.get("name", ""),
            "updated_at": item.get("updated_at", 0),
            "entries": [asdict(e) for e in item.get("entries", [])],
        }

    def describe(self) -> list[dict[str, Any]]:
        return [self.describe_mode(mode_id) for mode_id in MODES]

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise RuntimeError(f"预设文件 {self.path} 无法读取: {e}") from e
        data: dict[str, dict[str, Any]] = {}
        for mode_id, item in (raw.items() if isinstance(raw, dict) else ()):
            if mode_id not in MODES or not isinstance(item, dict):
                continue
            with contextlib.suppress(PresetError, TypeError, ValueError):
                data[mode_id] = {
                    "name": str(item.get("name") or "")[:MAX_NAME],
                    "entries": clean_entries(item.get("entries") or []),
                    "updated_at": float(item.get("updated_at") or 0),
                }
        return data

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        serializable = {
            mode_id: {**item, "entries": [asdict(e) for e in item["entries"]]} for mode_id, item in data.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".presets-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
            with contextlib.suppress(OSError):
                Path(tmp).chmod(0o600)
            Path(tmp).replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise
