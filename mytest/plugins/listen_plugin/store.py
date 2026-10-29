"""SQLite 存储：会话、用过的登录链接、房间快照、平台凭证（密文）、喜欢的音乐、导入的歌单。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
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
-- 平台登录凭证，AES-GCM 加密（crypto.py）。owner 是 QQ 号；bot 自己的会员账号 owner 为 "bot"。
CREATE TABLE IF NOT EXISTS credentials (
    owner TEXT NOT NULL,
    platform TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    nonce BLOB NOT NULL,
    key_id TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    vip INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',        -- ok / expired
    status_message TEXT NOT NULL DEFAULT '',
    refreshed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner, platform)
);
-- 每人一个“喜欢的音乐”
CREATE TABLE IF NOT EXISTS favorites (
    qq TEXT NOT NULL,
    ref TEXT NOT NULL,
    track TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (qq, ref)
);
-- 从平台导入的歌单。歌曲列表存快照，每天同步一次。
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq TEXT NOT NULL,
    platform TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cover TEXT NOT NULL DEFAULT '',
    track_count INTEGER NOT NULL DEFAULT 0,
    tracks TEXT NOT NULL DEFAULT '[]',
    hidden INTEGER NOT NULL DEFAULT 0,         -- 1：仅自己可见
    via TEXT NOT NULL DEFAULT 'uid',           -- uid：填用户 ID 导入；login：扫码导入
    synced_at INTEGER NOT NULL DEFAULT 0,
    sync_error TEXT NOT NULL DEFAULT '',
    UNIQUE (qq, platform, remote_id)
);
-- 填用户 ID 导入时记下平台账号，每天按这个同步公开歌单
CREATE TABLE IF NOT EXISTS linked_accounts (
    qq TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (qq, platform)
);
"""

MAX_FAVORITES = 2000

_UPSERT_PLAYLIST_KEEP_HIDDEN = (
    "INSERT INTO playlists (qq, platform, remote_id, name, cover, track_count, tracks, hidden, via, synced_at,"
    " sync_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')"
    " ON CONFLICT(qq, platform, remote_id) DO UPDATE SET name = excluded.name, cover = excluded.cover,"
    " track_count = excluded.track_count, tracks = excluded.tracks, via = excluded.via,"
    " synced_at = excluded.synced_at, sync_error = ''"
)
# 新导入时连可见性一起写
_UPSERT_PLAYLIST = _UPSERT_PLAYLIST_KEEP_HIDDEN + ", hidden = excluded.hidden"


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

    # ------------------------------------------------------------ 凭证（密文）

    def save_credential(self, owner: str, platform: str, row: CredentialRow, now: int) -> None:
        self._run(
            "INSERT INTO credentials (owner, platform, ciphertext, nonce, key_id, account_id, account_name, vip,"
            " status, status_message, refreshed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', '', ?, ?)"
            " ON CONFLICT(owner, platform) DO UPDATE SET ciphertext = excluded.ciphertext, nonce = excluded.nonce,"
            " key_id = excluded.key_id, account_id = excluded.account_id, account_name = excluded.account_name,"
            " vip = excluded.vip, status = 'ok', status_message = '', refreshed_at = excluded.refreshed_at",
            (owner, platform, row.ciphertext, row.nonce, row.key_id, row.account_id, row.account_name,
             int(row.vip), now, now),
        )

    def credential(self, owner: str, platform: str) -> CredentialRow | None:
        row = self._run(
            "SELECT ciphertext, nonce, key_id, account_id, account_name, vip, status, status_message, refreshed_at"
            " FROM credentials WHERE owner = ? AND platform = ?",
            (owner, platform),
        ).fetchone()
        return CredentialRow(*row) if row else None

    def credentials(self) -> list[tuple[str, str, CredentialRow]]:
        rows = self._run(
            "SELECT owner, platform, ciphertext, nonce, key_id, account_id, account_name, vip, status, status_message,"
            " refreshed_at FROM credentials"
        ).fetchall()
        return [(r[0], r[1], CredentialRow(*r[2:])) for r in rows]

    def mark_credential(self, owner: str, platform: str, status: str, message: str = "") -> None:
        self._run(
            "UPDATE credentials SET status = ?, status_message = ? WHERE owner = ? AND platform = ?",
            (status, message[:200], owner, platform),
        )

    def delete_credential(self, owner: str, platform: str | None = None) -> None:
        if platform is None:
            self._run("DELETE FROM credentials WHERE owner = ?", (owner,))
        else:
            self._run("DELETE FROM credentials WHERE owner = ? AND platform = ?", (owner, platform))

    def credential_owners(self) -> list[str]:
        return [r[0] for r in self._run("SELECT DISTINCT owner FROM credentials WHERE owner != 'bot'").fetchall()]

    # ------------------------------------------------------------ 喜欢的音乐

    def add_favorite(self, qq: str, ref: str, track: dict[str, Any], now: int) -> bool:
        """加入喜欢的音乐。已经在里面返回 False；超过上限抛 ValueError。"""
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM favorites WHERE qq = ?", (qq,)).fetchone()[0]
            if count >= MAX_FAVORITES:
                raise ValueError(f"喜欢的音乐最多 {MAX_FAVORITES} 首")
            cur = self._db.execute(
                "INSERT OR IGNORE INTO favorites VALUES (?, ?, ?, ?)",
                (qq, ref, json.dumps(track, ensure_ascii=False), now),
            )
            self._db.commit()
            return cur.rowcount > 0

    def remove_favorite(self, qq: str, ref: str) -> None:
        self._run("DELETE FROM favorites WHERE qq = ? AND ref = ?", (qq, ref))

    def favorites(self, qq: str) -> list[dict[str, Any]]:
        rows = self._run("SELECT track FROM favorites WHERE qq = ? ORDER BY added_at DESC", (qq,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def favorite_refs(self, qq: str) -> set[str]:
        return {r[0] for r in self._run("SELECT ref FROM favorites WHERE qq = ?", (qq,)).fetchall()}

    # ------------------------------------------------------------ 导入的歌单

    def upsert_playlist(self, qq: str, platform: str, remote_id: str, *, name: str, cover: str, tracks: list[dict],
                        track_count: int, via: str, hidden: bool | None, now: int) -> int:
        """新增或更新一个歌单。hidden 为 None 时保留原来的可见性。返回本地 id。"""
        with self._lock:
            self._db.execute(
                _UPSERT_PLAYLIST if hidden is not None else _UPSERT_PLAYLIST_KEEP_HIDDEN,
                (qq, platform, remote_id, name[:200], cover, track_count, json.dumps(tracks, ensure_ascii=False),
                 int(bool(hidden)), via, now),
            )
            row = self._db.execute(
                "SELECT id FROM playlists WHERE qq = ? AND platform = ? AND remote_id = ?", (qq, platform, remote_id)
            ).fetchone()
            self._db.commit()
            return int(row[0])

    def playlists(self, qq: str, *, include_hidden: bool) -> list[dict[str, Any]]:
        sql = ("SELECT id, platform, remote_id, name, cover, track_count, hidden, via, synced_at, sync_error"
               " FROM playlists WHERE qq = ?")
        if not include_hidden:
            sql += " AND hidden = 0"
        rows = self._run(sql + " ORDER BY id", (qq,)).fetchall()
        keys = ("id", "platform", "remote_id", "name", "cover", "track_count", "hidden", "via", "synced_at",
                "sync_error")
        return [dict(zip(keys, r, strict=True)) | {"hidden": bool(r[6])} for r in rows]

    def playlist(self, playlist_id: int) -> dict[str, Any] | None:
        row = self._run(
            "SELECT id, qq, platform, remote_id, name, cover, track_count, tracks, hidden, via, synced_at, sync_error"
            " FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
        if not row:
            return None
        keys = ("id", "qq", "platform", "remote_id", "name", "cover", "track_count", "tracks", "hidden", "via",
                "synced_at", "sync_error")
        data: dict[str, Any] = dict(zip(keys, row, strict=True))
        data["tracks"] = json.loads(data["tracks"])
        data["hidden"] = bool(data["hidden"])
        return data

    def set_playlist_hidden(self, qq: str, playlist_id: int, hidden: bool) -> bool:
        cur = self._run("UPDATE playlists SET hidden = ? WHERE id = ? AND qq = ?", (int(hidden), playlist_id, qq))
        return cur.rowcount > 0

    def delete_playlist(self, qq: str, playlist_id: int) -> bool:
        return self._run("DELETE FROM playlists WHERE id = ? AND qq = ?", (playlist_id, qq)).rowcount > 0

    def mark_playlist_error(self, playlist_id: int, message: str) -> None:
        self._run("UPDATE playlists SET sync_error = ? WHERE id = ?", (message[:200], playlist_id))

    def playlists_to_sync(self, before: int) -> list[tuple[int, str, str, str, str]]:
        """上次同步早于 before 的歌单：(id, qq, platform, remote_id, via)。"""
        return [tuple(r) for r in self._run(
            "SELECT id, qq, platform, remote_id, via FROM playlists WHERE synced_at < ? ORDER BY synced_at", (before,)
        ).fetchall()]

    def link_account(self, qq: str, platform: str, account_id: str, account_name: str) -> None:
        self._run(
            "INSERT INTO linked_accounts VALUES (?, ?, ?, ?) ON CONFLICT(qq, platform) DO UPDATE SET"
            " account_id = excluded.account_id, account_name = excluded.account_name",
            (qq, platform, account_id, account_name[:100]),
        )

    def linked_account(self, qq: str, platform: str) -> tuple[str, str] | None:
        row = self._run(
            "SELECT account_id, account_name FROM linked_accounts WHERE qq = ? AND platform = ?", (qq, platform)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def unlink_account(self, qq: str, platform: str) -> None:
        self._run("DELETE FROM linked_accounts WHERE qq = ? AND platform = ?", (qq, platform))

    def forget_user(self, qq: str) -> None:
        """用户不再属于任何白名单群：凭证立即删除。歌单和喜欢的音乐保留，回来还能用。"""
        self.delete_credential(qq)


@dataclass(frozen=True)
class CredentialRow:
    ciphertext: bytes
    nonce: bytes
    key_id: str
    account_id: str = ""
    account_name: str = ""
    vip: bool = False
    status: str = "ok"
    status_message: str = ""
    refreshed_at: int = 0
