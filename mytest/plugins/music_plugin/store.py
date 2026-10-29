"""SQLite 存储：bot 会员账号的登录凭证（密文）。

表结构和一起听时期一样，原来的 listen.db 可以直接接着用（会员账号不用重新扫码）。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
-- 平台登录凭证，AES-GCM 加密（crypto.py）。bot 自己的会员账号 owner 为 "bot"。
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
"""


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

    def credentials(self, owner: str) -> list[tuple[str, CredentialRow]]:
        rows = self._run(
            "SELECT platform, ciphertext, nonce, key_id, account_id, account_name, vip, status, status_message,"
            " refreshed_at FROM credentials WHERE owner = ?",
            (owner,),
        ).fetchall()
        return [(r[0], CredentialRow(*r[1:])) for r in rows]

    def mark_credential(self, owner: str, platform: str, status: str, message: str = "") -> None:
        self._run(
            "UPDATE credentials SET status = ?, status_message = ? WHERE owner = ? AND platform = ?",
            (status, message[:200], owner, platform),
        )

    def delete_credential(self, owner: str, platform: str) -> None:
        self._run("DELETE FROM credentials WHERE owner = ? AND platform = ?", (owner, platform))
