"""跑团的 SQLite 存储。只管读写，规则在 campaign.py。不依赖 NoneBot。

所有事实都存在这里，模型每轮从这里读，不靠它自己记。
结算用到的表（round_roll、outbox、log、summary）在实现对应步骤时加上。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4

SCHEMA = """
-- 上传的模组（插件自带的模组不进这张表，直接读文件）
CREATE TABLE IF NOT EXISTS module (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    module_title TEXT NOT NULL,
    module_source TEXT NOT NULL,           -- 开团时的模组快照，之后改模组不影响进行中的团
    phase TEXT NOT NULL,                   -- recruiting / running / paused / ended
    opener TEXT NOT NULL,                  -- 开团人的 QQ 号
    scene TEXT NOT NULL DEFAULT '',
    round INTEGER NOT NULL DEFAULT 0,
    game_time TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,   -- 每次状态改变 +1，模型回复到达时版本变了就丢弃
    end_reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
-- 一个群同时只有一个没结束的团
CREATE UNIQUE INDEX IF NOT EXISTS campaign_active ON campaign(group_id) WHERE phase != 'ended';

CREATE TABLE IF NOT EXISTS campaign_player (
    campaign_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    occupation TEXT NOT NULL DEFAULT '',
    pregen TEXT NOT NULL DEFAULT '',       -- 用的预设角色名；空表示用自己的 .st 卡
    sheet TEXT NOT NULL,                   -- 技能和属性的原始值（JSON），团里单独一份，不改 .st 那张
    san INTEGER NOT NULL,
    luck INTEGER NOT NULL,
    hp INTEGER,                            -- 角色卡算不出来时为空
    items TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,                  -- joining / present / leaving / left
    joined_round INTEGER NOT NULL,
    left_round INTEGER,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, user_id)
);

CREATE TABLE IF NOT EXISTS campaign_flag (
    campaign_id INTEGER NOT NULL,
    flag TEXT NOT NULL,
    round INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, flag)
);

CREATE TABLE IF NOT EXISTS campaign_clue (
    campaign_id INTEGER NOT NULL,
    clue TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',      -- 谁拿到的
    round INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, clue)
);

CREATE TABLE IF NOT EXISTS round (
    campaign_id INTEGER NOT NULL,
    no INTEGER NOT NULL,
    phase TEXT NOT NULL,                   -- collecting（收集中）/ collected（收齐，结算中）/ done
    started_at INTEGER NOT NULL,
    clock_from INTEGER NOT NULL,           -- 超时从这个时间往后算；暂停后继续时重置
    asked_at INTEGER,                      -- 问过「还有人要行动吗」的时间
    collected_at INTEGER,
    standby TEXT NOT NULL DEFAULT '[]',    -- 超时没行动、按原地待命处理的 QQ 号
    data TEXT NOT NULL DEFAULT '{}',       -- 各步骤的输入输出
    PRIMARY KEY (campaign_id, no)
);

CREATE TABLE IF NOT EXISTS round_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,  -- 撤回或改成场外
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS round_action_round ON round_action(campaign_id, round_no);
CREATE UNIQUE INDEX IF NOT EXISTS round_action_message ON round_action(campaign_id, message_id);

CREATE TABLE IF NOT EXISTS campaign_npc (
    campaign_id INTEGER NOT NULL,
    npc TEXT NOT NULL,
    attitude TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',     -- 所在场景，空表示还没出现在任何场景
    appeared INTEGER NOT NULL DEFAULT 0,   -- 调查员见过没有
    secret_told INTEGER NOT NULL DEFAULT 0, -- 秘密已经当面说出来了（之后叙述里出现他的剧透关键词不算漏）
    PRIMARY KEY (campaign_id, npc)
);

-- 掷出的骰。只插入，永不修改：孤注一掷、花幸运都另记一行
CREATE TABLE IF NOT EXISTS round_roll (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- check / push / luck / sanity / idea
    skill TEXT NOT NULL,
    value INTEGER NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'regular',
    bonus INTEGER NOT NULL DEFAULT 0,
    roll INTEGER NOT NULL,
    target INTEGER NOT NULL,
    level TEXT NOT NULL,
    passed INTEGER NOT NULL,
    object TEXT NOT NULL DEFAULT '',       -- 检定的对象，判断重复检定用
    summary TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS round_roll_round ON round_roll(campaign_id, round_no);

CREATE TABLE IF NOT EXISTS campaign_event (
    campaign_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    round INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, event, round)
);

-- 检定失败后等玩家回答：孤注一掷 / 花幸运 / 放弃。有没回答的选择时，下一轮先不结算
CREATE TABLE IF NOT EXISTS pending_choice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    data TEXT NOT NULL,                    -- 原来的检定、裁决、意图（JSON）
    can_push INTEGER NOT NULL,
    luck_cost INTEGER,                     -- 花多少幸运能通过；不能花为空
    status TEXT NOT NULL DEFAULT 'open',   -- open / working（正在处理）/ done / abandoned / expired
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_choice_campaign ON pending_choice(campaign_id, status);

-- 模型调用和其他排查用的记录。visibility：kp（含秘密）/ public
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'kp',
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS log_campaign ON log(campaign_id, id);
"""

# 旧版本建的表缺的列：(表, 列, 定义)
MIGRATIONS = (
    ("campaign_npc", "secret_told", "INTEGER NOT NULL DEFAULT 0"),
)

CAMPAIGN_COLUMNS = (
    "id", "group_id", "module_id", "module_title", "phase", "opener", "scene", "round",
    "game_time", "revision", "end_reason", "created_at", "updated_at",
)
PLAYER_COLUMNS = (
    "campaign_id", "user_id", "name", "occupation", "pregen", "sheet", "san", "luck", "hp",
    "items", "status", "joined_round", "left_round", "joined_at",
)
ROUND_COLUMNS = ("campaign_id", "no", "phase", "started_at", "clock_from", "asked_at", "collected_at", "standby", "data")
ACTION_COLUMNS = ("id", "round_no", "user_id", "message_id", "text", "withdrawn", "created_at")
CAMPAIGN_UPDATABLE = {"phase", "scene", "round", "game_time", "end_reason"}
PLAYER_UPDATABLE = {"status", "left_round", "san", "luck", "hp", "items"}
ROUND_UPDATABLE = {"phase", "clock_from", "asked_at", "collected_at", "standby", "data"}
NPC_UPDATABLE = {"attitude", "location", "appeared", "secret_told"}


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        with self._db:
            self._db.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                columns = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """一组写入要么全成功，要么全不改。"""
        with self._lock, self._db:
            yield self._db

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # ---------------------------------------------------------------- 模组

    def modules(self) -> list[sqlite3.Row]:
        return self._all("SELECT id, title, source, uploaded_at FROM module ORDER BY uploaded_at, id")

    def save_module(self, module_id: str, title: str, source: str, now: int) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO module (id, title, source, uploaded_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET title = excluded.title, source = excluded.source,"
                " uploaded_at = excluded.uploaded_at",
                (module_id, title, source, now),
            )

    def delete_module(self, module_id: str) -> bool:
        with self.transaction() as db:
            return db.execute("DELETE FROM module WHERE id = ?", (module_id,)).rowcount > 0

    # ---------------------------------------------------------------- 团

    def insert_campaign(self, group_id: str, module_id: str, title: str, source: str, opener: str, now: int) -> int:
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO campaign (group_id, module_id, module_title, module_source, phase, opener,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'recruiting', ?, ?, ?)",
                (group_id, module_id, title, source, opener, now, now),
            )
            return int(cur.lastrowid)

    def campaign(self, campaign_id: int) -> sqlite3.Row | None:
        return self._one(f"SELECT {', '.join(CAMPAIGN_COLUMNS)} FROM campaign WHERE id = ?", (campaign_id,))

    def active_campaign(self, group_id: str) -> sqlite3.Row | None:
        return self._one(
            f"SELECT {', '.join(CAMPAIGN_COLUMNS)} FROM campaign WHERE group_id = ? AND phase != 'ended'",
            (group_id,),
        )

    def unfinished_campaigns(self) -> list[sqlite3.Row]:
        return self._all(f"SELECT {', '.join(CAMPAIGN_COLUMNS)} FROM campaign WHERE phase != 'ended' ORDER BY id")

    def module_source(self, campaign_id: int) -> str:
        row = self._one("SELECT module_source FROM campaign WHERE id = ?", (campaign_id,))
        return row[0] if row else ""

    def update_campaign(self, db: sqlite3.Connection, campaign_id: int, now: int, **fields: Any) -> None:
        """在 transaction() 里调用。版本号每次 +1。"""
        unknown = set(fields) - CAMPAIGN_UPDATABLE
        if unknown:
            raise ValueError(f"campaign 不能改这些字段：{sorted(unknown)}")
        sets = "".join(f"{k} = ?, " for k in fields)
        db.execute(
            f"UPDATE campaign SET {sets}revision = revision + 1, updated_at = ? WHERE id = ?",
            (*fields.values(), now, campaign_id),
        )

    def bump_revision(self, db: sqlite3.Connection, campaign_id: int, now: int) -> None:
        self.update_campaign(db, campaign_id, now)

    # ---------------------------------------------------------------- 调查员

    def players(self, campaign_id: int) -> list[sqlite3.Row]:
        return self._all(
            f"SELECT {', '.join(PLAYER_COLUMNS)} FROM campaign_player WHERE campaign_id = ? ORDER BY joined_at, rowid",
            (campaign_id,),
        )

    def player(self, campaign_id: int, user_id: str) -> sqlite3.Row | None:
        return self._one(
            f"SELECT {', '.join(PLAYER_COLUMNS)} FROM campaign_player WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, user_id),
        )

    def upsert_player(self, db: sqlite3.Connection, campaign_id: int, user_id: str, *, name: str,
                      occupation: str, pregen: str, sheet: dict[str, int], san: int, luck: int,
                      hp: int | None, status: str, joined_round: int, now: int) -> None:
        """加入；离开过的人重新加入时整行换成新的角色。"""
        db.execute(
            "INSERT INTO campaign_player (campaign_id, user_id, name, occupation, pregen, sheet, san, luck, hp,"
            " status, joined_round, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(campaign_id, user_id) DO UPDATE SET name = excluded.name,"
            " occupation = excluded.occupation, pregen = excluded.pregen, sheet = excluded.sheet,"
            " san = excluded.san, luck = excluded.luck, hp = excluded.hp, items = '[]',"
            " status = excluded.status, joined_round = excluded.joined_round, left_round = NULL,"
            " joined_at = excluded.joined_at",
            (campaign_id, user_id, name, occupation, pregen, json.dumps(sheet, ensure_ascii=False),
             san, luck, hp, status, joined_round, now),
        )

    def update_player(self, db: sqlite3.Connection, campaign_id: int, user_id: str, **fields: Any) -> None:
        unknown = set(fields) - PLAYER_UPDATABLE
        if unknown:
            raise ValueError(f"campaign_player 不能改这些字段：{sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(
            f"UPDATE campaign_player SET {sets} WHERE campaign_id = ? AND user_id = ?",
            (*fields.values(), campaign_id, user_id),
        )

    def delete_player(self, db: sqlite3.Connection, campaign_id: int, user_id: str) -> None:
        db.execute("DELETE FROM campaign_player WHERE campaign_id = ? AND user_id = ?", (campaign_id, user_id))

    # ---------------------------------------------------------------- 剧情状态

    def flags(self, campaign_id: int) -> set[str]:
        return {r[0] for r in self._all("SELECT flag FROM campaign_flag WHERE campaign_id = ?", (campaign_id,))}

    def set_flag(self, db: sqlite3.Connection, campaign_id: int, flag: str, round_no: int) -> None:
        db.execute("INSERT OR IGNORE INTO campaign_flag (campaign_id, flag, round) VALUES (?, ?, ?)",
                   (campaign_id, flag, round_no))

    def clues(self, campaign_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT clue, user_id, round FROM campaign_clue WHERE campaign_id = ? ORDER BY round, rowid",
                         (campaign_id,))

    def reveal_clue(self, db: sqlite3.Connection, campaign_id: int, clue: str, user_id: str, round_no: int) -> bool:
        cur = db.execute("INSERT OR IGNORE INTO campaign_clue (campaign_id, clue, user_id, round) VALUES (?, ?, ?, ?)",
                         (campaign_id, clue, user_id, round_no))
        return cur.rowcount > 0

    # ---------------------------------------------------------------- 轮次和行动

    def round(self, campaign_id: int, no: int) -> sqlite3.Row | None:
        return self._one(f"SELECT {', '.join(ROUND_COLUMNS)} FROM round WHERE campaign_id = ? AND no = ?",
                         (campaign_id, no))

    def insert_round(self, db: sqlite3.Connection, campaign_id: int, no: int, now: int) -> None:
        db.execute("INSERT OR IGNORE INTO round (campaign_id, no, phase, started_at, clock_from)"
                   " VALUES (?, ?, 'collecting', ?, ?)", (campaign_id, no, now, now))

    def update_round(self, db: sqlite3.Connection, campaign_id: int, no: int, **fields: Any) -> None:
        unknown = set(fields) - ROUND_UPDATABLE
        if unknown:
            raise ValueError(f"round 不能改这些字段：{sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE round SET {sets} WHERE campaign_id = ? AND no = ?", (*fields.values(), campaign_id, no))

    def actions(self, campaign_id: int, round_no: int) -> list[sqlite3.Row]:
        return self._all(
            f"SELECT {', '.join(ACTION_COLUMNS)} FROM round_action WHERE campaign_id = ? AND round_no = ? ORDER BY id",
            (campaign_id, round_no),
        )

    def action_by_message(self, campaign_id: int, message_id: str) -> sqlite3.Row | None:
        return self._one(
            f"SELECT {', '.join(ACTION_COLUMNS)} FROM round_action WHERE campaign_id = ? AND message_id = ?",
            (campaign_id, message_id),
        )

    def insert_action(self, db: sqlite3.Connection, campaign_id: int, round_no: int, user_id: str,
                      message_id: str, text: str, now: int) -> bool:
        """同一条消息只记一次（重启后 QQ 重发的消息不会重复）。"""
        cur = db.execute(
            "INSERT OR IGNORE INTO round_action (campaign_id, round_no, user_id, message_id, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, round_no, user_id, message_id, text, now),
        )
        return cur.rowcount > 0

    def withdraw_action(self, db: sqlite3.Connection, action_id: int) -> None:
        db.execute("UPDATE round_action SET withdrawn = 1 WHERE id = ?", (action_id,))

    def npcs(self, campaign_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT npc, attitude, location, appeared, secret_told FROM campaign_npc"
                         " WHERE campaign_id = ? ORDER BY rowid", (campaign_id,))

    def update_npc(self, db: sqlite3.Connection, campaign_id: int, npc: str, **fields: Any) -> None:
        unknown = set(fields) - NPC_UPDATABLE
        if unknown:
            raise ValueError(f"campaign_npc 不能改这些字段：{sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE campaign_npc SET {sets} WHERE campaign_id = ? AND npc = ?",
                   (*fields.values(), campaign_id, npc))

    # ---------------------------------------------------------------- 骰子、事件、记录

    def insert_roll(self, db: sqlite3.Connection, campaign_id: int, round_no: int, user_id: str, kind: str, *,
                    skill: str, value: int, difficulty: str, bonus: int, roll: int, target: int, level: str,
                    passed: bool, object: str = "", summary: str = "", now: int) -> int:
        cur = db.execute(
            "INSERT INTO round_roll (campaign_id, round_no, user_id, kind, skill, value, difficulty, bonus, roll,"
            " target, level, passed, object, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, round_no, user_id, kind, skill, value, difficulty, bonus, roll, target, level,
             int(passed), object[:100], summary[:300], now),
        )
        return int(cur.lastrowid)

    def rolls(self, campaign_id: int, since_round: int = 0) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM round_roll WHERE campaign_id = ? AND round_no >= ? ORDER BY id",
                         (campaign_id, since_round))

    def fired_events(self, campaign_id: int) -> set[str]:
        return {r[0] for r in self._all("SELECT event FROM campaign_event WHERE campaign_id = ?", (campaign_id,))}

    def fire_event(self, db: sqlite3.Connection, campaign_id: int, event: str, round_no: int) -> None:
        db.execute("INSERT OR IGNORE INTO campaign_event (campaign_id, event, round) VALUES (?, ?, ?)",
                   (campaign_id, event, round_no))

    def add_log(self, campaign_id: int, round_no: int, kind: str, data: Any, now: int, visibility: str = "kp") -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO log (campaign_id, round_no, kind, visibility, data, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (campaign_id, round_no, kind, visibility, json.dumps(data, ensure_ascii=False), now))

    def logs(self, campaign_id: int, kind: str = "", limit: int = 100) -> list[sqlite3.Row]:
        if kind:
            return self._all("SELECT * FROM log WHERE campaign_id = ? AND kind = ? ORDER BY id DESC LIMIT ?",
                             (campaign_id, kind, limit))
        return self._all("SELECT * FROM log WHERE campaign_id = ? ORDER BY id DESC LIMIT ?", (campaign_id, limit))

    # ---------------------------------------------------------------- 待回答的选择

    def insert_choice(self, db: sqlite3.Connection, campaign_id: int, round_no: int, user_id: str, data: Any,
                      can_push: bool, luck_cost: int | None, now: int, expires_at: int) -> None:
        db.execute(
            "INSERT INTO pending_choice (campaign_id, round_no, user_id, data, can_push, luck_cost, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, round_no, user_id, json.dumps(data, ensure_ascii=False), int(can_push), luck_cost, now,
             expires_at),
        )

    def open_choices(self, campaign_id: int | None = None) -> list[sqlite3.Row]:
        """还没回答完的（open 和 working）。"""
        if campaign_id is None:
            return self._all("SELECT * FROM pending_choice WHERE status IN ('open', 'working') ORDER BY id")
        return self._all("SELECT * FROM pending_choice WHERE campaign_id = ? AND status IN ('open', 'working')"
                         " ORDER BY id", (campaign_id,))

    def set_choice_status(self, choice_id: int, status: str, *, only_from: tuple[str, ...] = ()) -> bool:
        """改状态；only_from 不为空时只在当前状态是其中之一时才改（防止两边同时处理）。"""
        with self.transaction() as db:
            if only_from:
                marks = ", ".join("?" for _ in only_from)
                cur = db.execute(f"UPDATE pending_choice SET status = ? WHERE id = ? AND status IN ({marks})",
                                 (status, choice_id, *only_from))
            else:
                cur = db.execute("UPDATE pending_choice SET status = ? WHERE id = ?", (status, choice_id))
            return cur.rowcount > 0

    def reset_working_choices(self) -> None:
        """重启时，处理到一半的选择回到等回答。"""
        with self.transaction() as db:
            db.execute("UPDATE pending_choice SET status = 'open' WHERE status = 'working'")

    def public_narrations(self, campaign_id: int, limit: int) -> list[str]:
        """最近几段发到群里的叙述，从旧到新。"""
        rows = self._all("SELECT data FROM log WHERE campaign_id = ? AND kind = 'narration' AND visibility = 'public'"
                         " ORDER BY id DESC LIMIT ?", (campaign_id, limit))
        return [json.loads(r[0]).get("text", "") for r in reversed(rows)]

    def recent_rounds(self, campaign_id: int, before: int, limit: int) -> list[sqlite3.Row]:
        """第 before 轮之前已经结算完的几轮，从新到旧。"""
        return self._all(
            f"SELECT {', '.join(ROUND_COLUMNS)} FROM round WHERE campaign_id = ? AND no < ? AND phase = 'done'"
            " ORDER BY no DESC LIMIT ?", (campaign_id, before, limit))

    def insert_npc(self, db: sqlite3.Connection, campaign_id: int, npc: str, attitude: str, location: str) -> None:
        db.execute("INSERT OR IGNORE INTO campaign_npc (campaign_id, npc, attitude, location) VALUES (?, ?, ?, ?)",
                   (campaign_id, npc, attitude, location))
