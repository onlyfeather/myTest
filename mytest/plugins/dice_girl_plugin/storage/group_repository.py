"""群聊上下文、群员档案、昵称映射和群梗的持久化。

群员档案按 member_scope 隔离：
- "group:<群号>"：只在这个群里有效（默认）。
- "global"：所有群和私聊共享同一份档案。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .repository import get_connection

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        message_id TEXT,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        content TEXT NOT NULL,
        is_bot INTEGER NOT NULL DEFAULT 0,
        reply_to_user TEXT,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_messages_group_id ON group_messages(group_id, id)",
    """
    CREATE TABLE IF NOT EXISTS members (
        member_scope TEXT NOT NULL,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        impression TEXT NOT NULL DEFAULT '',
        facts TEXT NOT NULL DEFAULT '[]',
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (member_scope, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS member_aliases (
        member_scope TEXT NOT NULL,
        alias_key TEXT NOT NULL,
        alias TEXT NOT NULL,
        user_id TEXT NOT NULL,
        source TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (member_scope, alias_key, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_member_aliases_user ON member_aliases(member_scope, user_id)",
    """
    CREATE TABLE IF NOT EXISTS group_lore (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        keys TEXT NOT NULL,
        content TEXT NOT NULL,
        topic INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_lore_group ON group_lore(group_id)",
    """
    CREATE TABLE IF NOT EXISTS group_digest (
        group_id TEXT PRIMARY KEY,
        summary TEXT NOT NULL DEFAULT '',
        last_message_id INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        content TEXT NOT NULL,
        participants TEXT NOT NULL DEFAULT '[]',
        keywords TEXT NOT NULL DEFAULT '[]',
        importance INTEGER NOT NULL DEFAULT 3,
        created_at REAL NOT NULL,
        last_recalled_at REAL,
        recall_count INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        embedding BLOB,
        embedding_model TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_events_group ON group_events(group_id, archived, id)",
    """
    CREATE TABLE IF NOT EXISTS group_chat_settings (
        group_id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        topic_reply INTEGER NOT NULL DEFAULT 1
    )
    """,
]

# 同一个人最多保留的自动学习昵称数量，防止被刷爆。
MAX_AUTO_ALIASES = 8
# 手动设置的昵称不会被自动清理；自动来源按最近使用时间淘汰。
AUTO_SOURCES = ("card", "nickname", "learned")


def init_group_tables() -> None:
    with get_connection() as conn:
        for statement in SCHEMA:
            conn.execute(statement)


def normalize_alias(alias: str) -> str:
    return " ".join(str(alias).strip().casefold().split())


# ---------------------------------------------------------------- group messages


@dataclass
class GroupMessage:
    user_id: str
    display_name: str
    content: str
    is_bot: bool
    created_at: float
    reply_to_user: str | None = None
    message_id: str | None = None
    id: int = 0


def append_group_message(
    group_id: str,
    message: GroupMessage,
    keep_limit: int,
    retention_seconds: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO group_messages (
                group_id, message_id, user_id, display_name, content,
                is_bot, reply_to_user, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                message.message_id,
                message.user_id,
                message.display_name,
                message.content,
                int(message.is_bot),
                message.reply_to_user,
                message.created_at,
            ),
        )
        # 超过保留时长的消息，只有已经被整理进群记忆后才删除，避免冷清的群还没来得及总结就丢了。
        # 条数上限是硬上限，始终生效（总结一直失败时也不会无限增长）。
        row = conn.execute(
            "SELECT last_message_id FROM group_digest WHERE group_id = ?", (group_id,)
        ).fetchone()
        summarized_up_to = row[0] if row else 0
        conn.execute(
            """
            DELETE FROM group_messages
            WHERE group_id = ? AND (
                (created_at < ? AND id <= ?)
                OR id NOT IN (
                    SELECT id FROM group_messages WHERE group_id = ?
                    ORDER BY id DESC LIMIT ?
                )
            )
            """,
            (
                group_id,
                message.created_at - retention_seconds,
                summarized_up_to,
                group_id,
                keep_limit,
            ),
        )


_MESSAGE_COLUMNS = (
    "user_id, display_name, content, is_bot, created_at, reply_to_user, message_id, id"
)


def _row_to_message(row) -> GroupMessage:
    return GroupMessage(
        user_id=row[0],
        display_name=row[1],
        content=row[2],
        is_bot=bool(row[3]),
        created_at=row[4],
        reply_to_user=row[5],
        message_id=row[6],
        id=row[7],
    )


def load_group_messages(group_id: str, limit: int) -> list[GroupMessage]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM (
                SELECT * FROM group_messages WHERE group_id = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (group_id, limit),
        ).fetchall()
    return [_row_to_message(row) for row in rows]


def load_messages_after(group_id: str, after_id: int, limit: int) -> list[GroupMessage]:
    """按时间顺序返回 id > after_id 的最早 limit 条消息。"""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS} FROM group_messages
            WHERE group_id = ? AND id > ? ORDER BY id ASC LIMIT ?
            """,
            (group_id, after_id, limit),
        ).fetchall()
    return [_row_to_message(row) for row in rows]


def unsummarized_stats(group_id: str) -> tuple[int, float | None]:
    """(未整理的消息条数, 其中最早一条的时间)。"""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), MIN(created_at) FROM group_messages
            WHERE group_id = ? AND id > COALESCE(
                (SELECT last_message_id FROM group_digest WHERE group_id = ?), 0
            )
            """,
            (group_id, group_id),
        ).fetchone()
    return int(row[0]), row[1]


def clear_group_messages(group_id: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM group_messages WHERE group_id = ?", (group_id,)
        ).rowcount


# ---------------------------------------------------------------- members


@dataclass
class MemberProfile:
    member_scope: str
    user_id: str
    display_name: str = ""
    impression: str = ""
    facts: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    message_count: int = 0


def touch_member(
    member_scope: str,
    user_id: str,
    display_name: str,
    now: float,
    names: dict[str, str],
) -> None:
    """记录一次发言：更新显示名、计数，并登记群名片 / QQ 昵称为自动昵称。

    names: {source: alias}，例如 {"card": "王哥", "nickname": "Wang"}。
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO members (
                member_scope, user_id, display_name, first_seen, last_seen, message_count
            ) VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(member_scope, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen = excluded.last_seen,
                message_count = message_count + 1
            """,
            (member_scope, user_id, display_name, now, now),
        )
        for source, alias in names.items():
            _upsert_alias(conn, member_scope, user_id, alias, source, now)
        _trim_auto_aliases(conn, member_scope, user_id)


def _upsert_alias(conn, member_scope, user_id, alias, source, now) -> None:
    key = normalize_alias(alias)
    if not key:
        return
    existing = conn.execute(
        "SELECT source FROM member_aliases WHERE member_scope = ? AND alias_key = ? AND user_id = ?",
        (member_scope, key, user_id),
    ).fetchone()
    if existing and existing[0] == "manual" and source != "manual":
        # 手动设置的来源优先，自动来源只刷新时间。
        source = "manual"
    conn.execute(
        """
        INSERT INTO member_aliases (member_scope, alias_key, alias, user_id, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(member_scope, alias_key, user_id) DO UPDATE SET
            alias = excluded.alias,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (member_scope, key, alias.strip(), user_id, source, now),
    )


def _trim_auto_aliases(conn, member_scope, user_id) -> None:
    placeholders = ",".join("?" for _ in AUTO_SOURCES)
    conn.execute(
        f"""
        DELETE FROM member_aliases
        WHERE member_scope = ? AND user_id = ? AND source IN ({placeholders})
        AND alias_key NOT IN (
            SELECT alias_key FROM member_aliases
            WHERE member_scope = ? AND user_id = ? AND source IN ({placeholders})
            ORDER BY updated_at DESC LIMIT ?
        )
        """,
        (member_scope, user_id, *AUTO_SOURCES, member_scope, user_id, *AUTO_SOURCES, MAX_AUTO_ALIASES),
    )


def add_member_alias(member_scope: str, user_id: str, alias: str, source: str, now: float) -> None:
    with get_connection() as conn:
        _ensure_member(conn, member_scope, user_id, now)
        _upsert_alias(conn, member_scope, user_id, alias, source, now)
        if source in AUTO_SOURCES:
            _trim_auto_aliases(conn, member_scope, user_id)


def remove_member_alias(member_scope: str, user_id: str, alias: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM member_aliases WHERE member_scope = ? AND user_id = ? AND alias_key = ?",
            (member_scope, user_id, normalize_alias(alias)),
        ).rowcount


def _ensure_member(conn, member_scope, user_id, now) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO members (member_scope, user_id, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        """,
        (member_scope, user_id, now, now),
    )


def find_members_by_alias(member_scope: str, alias: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id FROM member_aliases
            WHERE member_scope = ? AND alias_key = ?
            ORDER BY CASE source WHEN 'manual' THEN 0 WHEN 'card' THEN 1 ELSE 2 END,
                     updated_at DESC
            """,
            (member_scope, normalize_alias(alias)),
        ).fetchall()
    return [row[0] for row in rows]


def load_alias_index(member_scope: str) -> dict[str, list[str]]:
    """{alias_key: [user_id, ...]}，用于在一段文本里找出被提到的人。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT alias_key, user_id FROM member_aliases WHERE member_scope = ?",
            (member_scope,),
        ).fetchall()
    index: dict[str, list[str]] = {}
    for key, user_id in rows:
        index.setdefault(key, []).append(user_id)
    return index


def get_member(member_scope: str, user_id: str) -> MemberProfile | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT display_name, impression, facts, first_seen, last_seen, message_count
            FROM members WHERE member_scope = ? AND user_id = ?
            """,
            (member_scope, user_id),
        ).fetchone()
        if not row:
            return None
        aliases = [
            r[0]
            for r in conn.execute(
                """
                SELECT alias FROM member_aliases WHERE member_scope = ? AND user_id = ?
                ORDER BY CASE source WHEN 'manual' THEN 0 WHEN 'card' THEN 1 ELSE 2 END,
                         updated_at DESC
                """,
                (member_scope, user_id),
            ).fetchall()
        ]
    return MemberProfile(
        member_scope=member_scope,
        user_id=user_id,
        display_name=row[0],
        impression=row[1],
        facts=_load_facts(row[2]),
        aliases=aliases,
        first_seen=row[3],
        last_seen=row[4],
        message_count=row[5],
    )


def update_member_profile(
    member_scope: str,
    user_id: str,
    *,
    impression: str | None = None,
    facts: list[str] | None = None,
    now: float | None = None,
) -> None:
    now = now or time.time()
    with get_connection() as conn:
        _ensure_member(conn, member_scope, user_id, now)
        if impression is not None:
            conn.execute(
                "UPDATE members SET impression = ? WHERE member_scope = ? AND user_id = ?",
                (impression, member_scope, user_id),
            )
        if facts is not None:
            conn.execute(
                "UPDATE members SET facts = ? WHERE member_scope = ? AND user_id = ?",
                (json.dumps(facts, ensure_ascii=False), member_scope, user_id),
            )


def forget_member(member_scope: str, user_id: str) -> int:
    with get_connection() as conn:
        removed = conn.execute(
            "DELETE FROM members WHERE member_scope = ? AND user_id = ?",
            (member_scope, user_id),
        ).rowcount
        conn.execute(
            "DELETE FROM member_aliases WHERE member_scope = ? AND user_id = ? AND source != 'manual'",
            (member_scope, user_id),
        )
    return removed


def _load_facts(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


# ---------------------------------------------------------------- group lore


@dataclass
class GroupLore:
    id: int
    keys: list[str]
    content: str
    topic: bool
    created_by: str


def add_group_lore(group_id: str, keys: list[str], content: str, created_by: str, topic: bool = True) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO group_lore (group_id, keys, content, topic, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (group_id, json.dumps(keys, ensure_ascii=False), content, int(topic), created_by, time.time()),
        )
        return int(cursor.lastrowid)


def list_group_lore(group_id: str) -> list[GroupLore]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, keys, content, topic, created_by FROM group_lore WHERE group_id = ? ORDER BY id",
            (group_id,),
        ).fetchall()
    return [
        GroupLore(id=row[0], keys=json.loads(row[1]), content=row[2], topic=bool(row[3]), created_by=row[4])
        for row in rows
    ]


def get_group_lore(group_id: str, lore_id: int) -> GroupLore | None:
    return next((item for item in list_group_lore(group_id) if item.id == lore_id), None)


def delete_group_lore(group_id: str, lore_id: int) -> int:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM group_lore WHERE group_id = ? AND id = ?",
            (group_id, lore_id),
        ).rowcount


# ---------------------------------------------------------------- group settings


@dataclass
class GroupChatSettings:
    enabled: bool = True
    topic_reply: bool = True


def get_group_chat_settings(group_id: str) -> GroupChatSettings:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled, topic_reply FROM group_chat_settings WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    if not row:
        return GroupChatSettings()
    return GroupChatSettings(enabled=bool(row[0]), topic_reply=bool(row[1]))


def set_group_chat_setting(group_id: str, name: str, value: bool) -> None:
    if name not in {"enabled", "topic_reply"}:
        raise ValueError(name)
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO group_chat_settings (group_id) VALUES (?)", (group_id,))
        conn.execute(
            f"UPDATE group_chat_settings SET {name} = ? WHERE group_id = ?",
            (int(value), group_id),
        )


# ---------------------------------------------------------------- group memory


@dataclass
class GroupDigest:
    summary: str = ""
    last_message_id: int = 0
    updated_at: float = 0.0


@dataclass
class GroupEvent:
    id: int
    group_id: str
    content: str
    participants: list[str]
    keywords: list[str]
    importance: int
    created_at: float
    last_recalled_at: float | None
    recall_count: int
    embedding: list[float] | None = None
    embedding_model: str | None = None


def get_group_digest(group_id: str) -> GroupDigest:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT summary, last_message_id, updated_at FROM group_digest WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    return GroupDigest(*row) if row else GroupDigest()


def save_digest_and_events(
    group_id: str,
    summary: str,
    last_message_id: int,
    expected_last_id: int,
    events: list[dict],
    now: float,
) -> bool:
    """在一个事务里写入新的近期群况和事件，并推进已整理位置。

    expected_last_id 用来防止两次整理并发时重复写入：位置已经被别人推进过就放弃。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_message_id FROM group_digest WHERE group_id = ?", (group_id,)
        ).fetchone()
        if (row[0] if row else 0) != expected_last_id:
            return False
        conn.execute(
            """
            INSERT INTO group_digest (group_id, summary, last_message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                summary = excluded.summary,
                last_message_id = excluded.last_message_id,
                updated_at = excluded.updated_at
            """,
            (group_id, summary, last_message_id, now),
        )
        for event in events:
            conn.execute(
                """
                INSERT INTO group_events (
                    group_id, content, participants, keywords, importance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    event["content"],
                    json.dumps(event.get("participants", []), ensure_ascii=False),
                    json.dumps(event.get("keywords", []), ensure_ascii=False),
                    int(event.get("importance", 3)),
                    now,
                ),
            )
    return True


def _row_to_event(row) -> GroupEvent:
    return GroupEvent(
        id=row[0],
        group_id=row[1],
        content=row[2],
        participants=_load_facts(row[3]),
        keywords=_load_facts(row[4]),
        importance=row[5],
        created_at=row[6],
        last_recalled_at=row[7],
        recall_count=row[8],
        embedding=unpack_vector(row[9]) if row[9] else None,
        embedding_model=row[10],
    )


_EVENT_COLUMNS = (
    "id, group_id, content, participants, keywords, importance, created_at, "
    "last_recalled_at, recall_count, embedding, embedding_model"
)


def list_group_events(group_id: str, include_archived: bool = False) -> list[GroupEvent]:
    archived_clause = "" if include_archived else "AND archived = 0"
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM group_events WHERE group_id = ? {archived_clause} ORDER BY id",
            (group_id,),
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def events_missing_embedding(model: str, limit: int) -> list[GroupEvent]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_EVENT_COLUMNS} FROM group_events
            WHERE archived = 0 AND (embedding IS NULL OR embedding_model IS NOT ?)
            ORDER BY id LIMIT ?
            """,
            (model, limit),
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def set_event_embedding(event_id: int, vector: list[float], model: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE group_events SET embedding = ?, embedding_model = ? WHERE id = ?",
            (pack_vector(vector), model, event_id),
        )


def mark_events_recalled(event_ids: list[int], now: float) -> None:
    if not event_ids:
        return
    with get_connection() as conn:
        conn.executemany(
            "UPDATE group_events SET last_recalled_at = ?, recall_count = recall_count + 1 WHERE id = ?",
            [(now, event_id) for event_id in event_ids],
        )


def archive_group_event(group_id: str, event_id: int) -> int:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE group_events SET archived = 1 WHERE group_id = ? AND id = ? AND archived = 0",
            (group_id, event_id),
        ).rowcount


def archive_stale_events(group_id: str, keep: int) -> int:
    """活跃事件超过 keep 条时，按保留价值从低到高归档多出的部分。"""
    events = list_group_events(group_id)
    if len(events) <= keep:
        return 0
    from ..group.memory_score import retention_score

    now = time.time()
    events.sort(key=lambda e: retention_score(e, now))
    victims = [e.id for e in events[: len(events) - keep]]
    with get_connection() as conn:
        conn.executemany(
            "UPDATE group_events SET archived = 1 WHERE id = ?",
            [(event_id,) for event_id in victims],
        )
    return len(victims)


def clear_group_memory(group_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM group_events WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM group_digest WHERE group_id = ?", (group_id,))


def lore_exists(group_id: str, keys: list[str]) -> bool:
    wanted = {normalize_alias(k) for k in keys}
    return any(wanted & {normalize_alias(k) for k in item.keys} for item in list_group_lore(group_id))


def pack_vector(vector: list[float]) -> bytes:
    import array

    return array.array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    import array

    values = array.array("f")
    values.frombytes(blob)
    return values.tolist()