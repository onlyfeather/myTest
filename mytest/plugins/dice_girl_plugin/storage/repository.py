import sqlite3
from contextlib import contextmanager
from datetime import datetime

from ..config import get_data_path

DB_PATH = get_data_path("dice_data.db")


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                favorability INTEGER DEFAULT 50,
                interaction_count INTEGER DEFAULT 0
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id TEXT PRIMARY KEY,
                assigned_role TEXT
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_private_settings (
                user_id TEXT PRIMARY KEY,
                assigned_role TEXT
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_role_affinity (
                user_id TEXT,
                role_id TEXT,
                favorability INTEGER DEFAULT 50,
                interaction_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, role_id)
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                role_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                message_role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_key_id
            ON chat_messages(scope, role_id, user_id, id)
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_summaries (
                scope TEXT NOT NULL,
                role_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, role_id, user_id)
            )
        """
        )


init_db()


def get_group_role(group_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT assigned_role FROM group_settings WHERE group_id = ?",
            (group_id,),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def set_group_role(group_id: str, role_key: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO group_settings (group_id, assigned_role) VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET assigned_role = ?
        """,
            (group_id, role_key, role_key),
        )


def get_private_role(user_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT assigned_role FROM user_private_settings WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def set_private_role(user_id: str, role_key: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_private_settings (user_id, assigned_role) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET assigned_role = ?
        """,
            (user_id, role_key, role_key),
        )


def get_user_favorability(user_id: str, role_id: str = "ling") -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT favorability FROM user_role_affinity "
            "WHERE user_id = ? AND role_id = ?",
            (user_id, role_id),
        )
        row = cursor.fetchone()

    if row:
        return row[0]
    return 50


def update_user_favorability(user_id: str, delta: int, role_id: str = "ling"):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT favorability FROM user_role_affinity "
            "WHERE user_id = ? AND role_id = ?",
            (user_id, role_id),
        )
        row = cursor.fetchone()
        current = row[0] if row else 50
        new_fav = max(0, min(100, current + delta))

        cursor.execute(
            """
            INSERT INTO user_role_affinity (
                user_id, role_id, favorability, interaction_count
            )
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, role_id) DO UPDATE SET
                favorability = ?,
                interaction_count = interaction_count + 1
        """,
            (user_id, role_id, new_fav, new_fav),
        )
    return new_fav


def get_user_stats(user_id: str, role_id: str = "ling"):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT favorability, interaction_count FROM user_role_affinity "
            "WHERE user_id = ? AND role_id = ?",
            (user_id, role_id),
        )
        row = cursor.fetchone()
    return {"fav": row[0], "count": row[1]} if row else {"fav": 50, "count": 0}


def set_user_favorability(user_id: str, value: int, role_id: str = "ling"):
    val = max(0, min(100, value))
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_role_affinity (user_id, role_id, favorability, interaction_count)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id, role_id) DO UPDATE SET favorability = ?
        """,
            (user_id, role_id, val, val),
        )


def load_chat_messages(
    scope: str,
    role_id: str,
    user_id: str,
    limit: int,
) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT message_role, content
            FROM (
                SELECT id, message_role, content
                FROM chat_messages
                WHERE scope = ? AND role_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
        """,
            (scope, role_id, user_id, limit),
        )
        rows = cursor.fetchall()
    return [{"role": row[0], "content": row[1]} for row in rows]


def append_chat_message(
    scope: str,
    role_id: str,
    user_id: str,
    message_role: str,
    content: str,
    keep_limit: int,
) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO chat_messages (
                scope, role_id, user_id, message_role, content, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                scope,
                role_id,
                user_id,
                message_role,
                content,
                datetime.now().isoformat(),
            ),
        )
        cursor.execute(
            """
            DELETE FROM chat_messages
            WHERE scope = ? AND role_id = ? AND user_id = ?
            AND id NOT IN (
                SELECT id FROM chat_messages
                WHERE scope = ? AND role_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
        """,
            (scope, role_id, user_id, scope, role_id, user_id, keep_limit),
        )


def clear_chat_messages(
    scope: str | None = None,
    role_id: str | None = None,
    user_id: str | None = None,
) -> int:
    clauses = []
    values = []
    if scope is not None:
        clauses.append("scope = ?")
        values.append(scope)
    if role_id is not None:
        clauses.append("role_id = ?")
        values.append(role_id)
    if user_id is not None:
        clauses.append("user_id = ?")
        values.append(user_id)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM chat_messages{where}", tuple(values))
        return cursor.rowcount


def count_chat_messages(
    scope: str | None = None,
    role_id: str | None = None,
    user_id: str | None = None,
) -> int:
    clauses = []
    values = []
    if scope is not None:
        clauses.append("scope = ?")
        values.append(scope)
    if role_id is not None:
        clauses.append("role_id = ?")
        values.append(role_id)
    if user_id is not None:
        clauses.append("user_id = ?")
        values.append(user_id)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM chat_messages{where}", tuple(values))
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def get_chat_summary(scope: str, role_id: str, user_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT summary FROM chat_summaries
            WHERE scope = ? AND role_id = ? AND user_id = ?
        """,
            (scope, role_id, user_id),
        )
        row = cursor.fetchone()
    return row[0] if row else ""


def set_chat_summary(scope: str, role_id: str, user_id: str, summary: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO chat_summaries (scope, role_id, user_id, summary, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, role_id, user_id) DO UPDATE SET
                summary = excluded.summary,
                updated_at = excluded.updated_at
        """,
            (scope, role_id, user_id, summary, datetime.now().isoformat()),
        )


def clear_chat_summaries(
    scope: str | None = None,
    role_id: str | None = None,
    user_id: str | None = None,
) -> int:
    clauses = []
    values = []
    if scope is not None:
        clauses.append("scope = ?")
        values.append(scope)
    if role_id is not None:
        clauses.append("role_id = ?")
        values.append(role_id)
    if user_id is not None:
        clauses.append("user_id = ?")
        values.append(user_id)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM chat_summaries{where}", tuple(values))
        return cursor.rowcount
