"""世界书：文件里的全局 / 角色深层设定 + 数据库里按群隔离的群梗。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..storage import group_repository as repo

MAX_INJECTED = 3


@dataclass
class LoreEntry:
    keys: list[str]
    content: str
    roles: list[str] | None = None
    scene: str = "all"
    topic: bool = False
    source: str = "file"  # file | group
    entry_id: str = ""


def parse_file_entries(raw: dict) -> list[LoreEntry]:
    entries = []
    for index, item in enumerate(raw.get("entries", [])):
        keys = [str(k).strip() for k in item.get("keys", []) if str(k).strip()]
        content = str(item.get("content", "")).strip()
        if not keys or not content:
            continue
        roles = item.get("roles")
        entries.append(
            LoreEntry(
                keys=keys,
                content=content,
                roles=list(roles) if roles else None,
                scene=item.get("scene", "all"),
                topic=bool(item.get("topic", False)),
                source="file",
                entry_id=f"file:{index}",
            )
        )
    return entries


def group_entries(group_id: str) -> list[LoreEntry]:
    return [
        LoreEntry(
            keys=item.keys,
            content=item.content,
            topic=item.topic,
            scene="group",
            source="group",
            entry_id=f"group:{group_id}:{item.id}",
        )
        for item in repo.list_group_lore(group_id)
    ]


def keyword_hit(text: str, keyword: str) -> bool:
    key = keyword.casefold()
    lowered = text.casefold()
    if re.fullmatch(r"[0-9a-z_\-]+", key):
        return re.search(rf"(?<![0-9a-z_]){re.escape(key)}(?![0-9a-z_])", lowered) is not None
    return key in lowered


def match_lore(
    entries: list[LoreEntry],
    text: str,
    role_id: str,
    scene: str,
) -> list[LoreEntry]:
    """按 群梗 > 角色专属 > 全局 的顺序返回命中的词条，最多 MAX_INJECTED 条。"""
    if not text:
        return []
    hits = []
    for entry in entries:
        if entry.roles and role_id not in entry.roles:
            continue
        if entry.scene not in ("all", scene):
            continue
        if any(keyword_hit(text, key) for key in entry.keys):
            hits.append(entry)

    def rank(entry: LoreEntry) -> int:
        if entry.source == "group":
            return 0
        return 1 if entry.roles else 2

    hits.sort(key=rank)
    return hits[:MAX_INJECTED]


def format_lore(entries: list[LoreEntry]) -> str:
    if not entries:
        return "- 无触发。"
    return "\n".join(f"- {entry.content}" for entry in entries)
