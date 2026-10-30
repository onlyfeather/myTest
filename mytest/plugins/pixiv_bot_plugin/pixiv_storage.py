import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from .config import get_data_path


class PixivStorage:
    """SQLite storage for Pixiv runtime state."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_data_path("pixiv_data.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_cookie (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    full_cookie TEXT NOT NULL,
                    user_id TEXT,
                    saved_time TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_tags (
                    kind TEXT NOT NULL CHECK (kind IN ('preferred', 'blocked')),
                    tag TEXT NOT NULL,
                    PRIMARY KEY (kind, tag)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_favorite_authors (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    added_time TEXT NOT NULL,
                    last_check TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_author_aliases (
                    alias TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES pixiv_favorite_authors(user_id)
                        ON DELETE CASCADE
                )
                """
            )
            # 关注作者更新推送：每个作者推送过的最新作品 ID。
            # 不加外键：replace_favorite_authors 会删掉重建作者表，外键级联会把这里一起清空。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_author_seen (
                    user_id TEXT PRIMARY KEY,
                    last_illust_id INTEGER NOT NULL,
                    checked_at TEXT NOT NULL
                )
                """
            )
            # 每日一图：每人每天选定的那张。重启后还是同一张；同一个人不会在 30 天内抽到重复的
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_daily (
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    illust_id TEXT NOT NULL,
                    tag TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL,
                    PRIMARY KEY (user_id, day)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pixiv_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM pixiv_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pixiv_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def save_cookie(self, full_cookie: str, user_id: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pixiv_cookie (id, full_cookie, user_id, saved_time)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    full_cookie = excluded.full_cookie,
                    user_id = excluded.user_id,
                    saved_time = excluded.saved_time
                """,
                (full_cookie, user_id, datetime.now().isoformat()),
            )

    def delete_cookie(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pixiv_cookie WHERE id = 1")

    def load_cookie(self) -> Optional[Dict[str, Optional[str]]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT full_cookie, user_id, saved_time FROM pixiv_cookie WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def load_tags(self, kind: str) -> Set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tag FROM pixiv_tags WHERE kind = ? ORDER BY tag",
                (kind,),
            ).fetchall()
        return {row["tag"] for row in rows}

    def replace_tags(self, kind: str, tags: Iterable[str]) -> None:
        clean_tags = sorted({tag.strip() for tag in tags if tag and tag.strip()})
        with self._connect() as conn:
            conn.execute("DELETE FROM pixiv_tags WHERE kind = ?", (kind,))
            conn.executemany(
                "INSERT INTO pixiv_tags (kind, tag) VALUES (?, ?)",
                [(kind, tag) for tag in clean_tags],
            )
            conn.execute(
                """
                INSERT INTO pixiv_settings (key, value)
                VALUES (?, '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"{kind}_tags_initialized",),
            )

    def load_favorite_authors(self) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            author_rows = conn.execute(
                """
                SELECT user_id, name, added_time, last_check
                FROM pixiv_favorite_authors
                ORDER BY added_time DESC, user_id
                """
            ).fetchall()
            alias_rows = conn.execute(
                "SELECT user_id, alias FROM pixiv_author_aliases ORDER BY alias"
            ).fetchall()

        aliases_by_user: Dict[str, list[str]] = {}
        for row in alias_rows:
            aliases_by_user.setdefault(row["user_id"], []).append(row["alias"])

        return {
            row["user_id"]: {
                "name": row["name"],
                "aliases": aliases_by_user.get(row["user_id"], []),
                "added_time": row["added_time"],
                "last_check": row["last_check"],
            }
            for row in author_rows
        }

    def replace_favorite_authors(self, authors: Dict[str, Dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pixiv_author_aliases")
            conn.execute("DELETE FROM pixiv_favorite_authors")
            for user_id, info in authors.items():
                conn.execute(
                    """
                    INSERT INTO pixiv_favorite_authors
                        (user_id, name, added_time, last_check)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(user_id),
                        str(info.get("name") or ""),
                        info.get("added_time") or datetime.now().isoformat(),
                        info.get("last_check"),
                    ),
                )
                aliases = {
                    alias.strip()
                    for alias in info.get("aliases", [])
                    if alias and alias.strip()
                }
                conn.executemany(
                    "INSERT OR REPLACE INTO pixiv_author_aliases (alias, user_id) VALUES (?, ?)",
                    [(alias, str(user_id)) for alias in sorted(aliases)],
                )

    def get_author_seen(self, user_id: str) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_illust_id FROM pixiv_author_seen WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return int(row["last_illust_id"]) if row else None

    def get_daily(self, user_id: str, day: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT illust_id, tag, data FROM pixiv_daily WHERE user_id = ? AND day = ?",
                (str(user_id), day),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data"])
        except ValueError:
            return None
        return {"illust_id": row["illust_id"], "tag": row["tag"], "image": data}

    def save_daily(self, user_id: str, day: str, illust_id: str, tag: str, image: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pixiv_daily (user_id, day, illust_id, tag, data) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET
                    illust_id = excluded.illust_id, tag = excluded.tag, data = excluded.data
                """,
                (str(user_id), day, str(illust_id), tag, json.dumps(image, ensure_ascii=False)),
            )

    def recent_daily_ids(self, user_id: str, since_day: str) -> set:
        """这个人从 since_day 起抽到过的作品，不让他短期内抽到重复的。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT illust_id FROM pixiv_daily WHERE user_id = ? AND day >= ?",
                (str(user_id), since_day),
            ).fetchall()
        return {row["illust_id"] for row in rows}

    def delete_daily_before(self, day: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pixiv_daily WHERE day < ?", (day,))

    def all_author_seen(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id, last_illust_id FROM pixiv_author_seen").fetchall()
        return {row["user_id"]: int(row["last_illust_id"]) for row in rows}

    def delete_author_seen(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pixiv_author_seen WHERE user_id = ?", (str(user_id),))

    def set_author_seen(self, user_id: str, last_illust_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pixiv_author_seen (user_id, last_illust_id, checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_illust_id = excluded.last_illust_id,
                    checked_at = excluded.checked_at
                """,
                (str(user_id), int(last_illust_id), datetime.now().isoformat()),
            )

    def update_author_last_check(self, user_id: str, checked_at: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pixiv_favorite_authors
                SET last_check = ?
                WHERE user_id = ?
                """,
                (checked_at, str(user_id)),
            )
            return cursor.rowcount > 0
