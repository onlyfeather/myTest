import sqlite3
import threading
from contextlib import contextmanager

from ..config import get_data_path

DB_PATH = get_data_path("dice_data.db")

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_connection():
    _ensure_initialized()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if not _initialized:
            init_db()
            _initialized = True


def init_db():
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        cursor = conn.cursor()

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

        conn.commit()
    finally:
        conn.close()


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


def get_user_favorability(user_id: str, role_id: str = "default") -> int:
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


def set_user_favorability(user_id: str, value: int, role_id: str = "default"):
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
