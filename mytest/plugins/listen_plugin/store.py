"""SQLite 存储：会话、用过的登录链接、房间快照。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    qq TEXT NOT NULL,
    group_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (qq, group_id);
CREATE TABLE IF NOT EXISTS used_links (
    jti TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    group_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    # ------------------------------------------------------------ 会话

    def add_session(self, token_hash: str, qq: str, group_id: str, expires_at: int) -> None:
        self._run("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash, qq, group_id, expires_at))

    def get_session(self, token_hash: str, now: int) -> tuple[str, str] | None:
        row = self._run(
            "SELECT qq, group_id FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def delete_session(self, token_hash: str) -> None:
        self._run("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_sessions_of(self, qq: str, group_id: str | None = None) -> None:
        if group_id is None:
            self._run("DELETE FROM sessions WHERE qq = ?", (qq,))
        else:
            self._run("DELETE FROM sessions WHERE qq = ? AND group_id = ?", (qq, group_id))

    def delete_sessions_in(self, group_id: str) -> None:
        self._run("DELETE FROM sessions WHERE group_id = ?", (group_id,))

    def delete_sessions_outside(self, groups: list[str]) -> None:
        marks = ",".join("?" * len(groups))
        if groups:
            self._run(f"DELETE FROM sessions WHERE group_id NOT IN ({marks})", tuple(groups))  # noqa: S608
        else:
            self._run("DELETE FROM sessions")

    # ------------------------------------------------------------ 一次性链接

    def use_link(self, jti: str, expires_at: int, now: int) -> bool:
        """记录链接已使用。已经用过时返回 False。"""
        with self._lock:
            self._db.execute("DELETE FROM used_links WHERE expires_at < ?", (now,))
            try:
                self._db.execute("INSERT INTO used_links VALUES (?, ?)", (jti, expires_at))
            except sqlite3.IntegrityError:
                self._db.commit()
                return False
            self._db.commit()
            return True

    def purge(self, now: int) -> None:
        self._run("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        self._run("DELETE FROM used_links WHERE expires_at < ?", (now,))

    # ------------------------------------------------------------ 房间

    def save_room(self, group_id: str, data: dict[str, Any], now: int) -> None:
        self._run(
            "INSERT INTO rooms VALUES (?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET data = excluded.data, "
            "updated_at = excluded.updated_at",
            (group_id, json.dumps(data, ensure_ascii=False), now),
        )

    def load_room(self, group_id: str) -> dict[str, Any] | None:
        row = self._run("SELECT data FROM rooms WHERE group_id = ?", (group_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None
