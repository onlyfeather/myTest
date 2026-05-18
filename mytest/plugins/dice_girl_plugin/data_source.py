import sqlite3
from contextlib import contextmanager

from .config import get_data_path

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

        # 1. 旧表 (保留以防万一，或者用于迁移)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                favorability INTEGER DEFAULT 50,
                interaction_count INTEGER DEFAULT 0
            )
        ''')

        # 2. 群组/私聊配置表 (保持不变)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id TEXT PRIMARY KEY,
                assigned_role TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_private_settings (
                user_id TEXT PRIMARY KEY,
                assigned_role TEXT
            )
        ''')

        # 3. 【新表】角色独立好感度表
        # 联合主键 (user_id, role_id)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_role_affinity (
                user_id TEXT,
                role_id TEXT,
                favorability INTEGER DEFAULT 50,
                interaction_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, role_id)
            )
        ''')


init_db()


# ================= 角色配置 Getter/Setter (保持不变) =================

def get_group_role(group_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT assigned_role FROM group_settings WHERE group_id = ?", (group_id,))
        row = cursor.fetchone()
    return row[0] if row else None


def set_group_role(group_id: str, role_key: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO group_settings (group_id, assigned_role) VALUES (?, ?) 
            ON CONFLICT(group_id) DO UPDATE SET assigned_role = ?
        """, (group_id, role_key, role_key))


def get_private_role(user_id: str) -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT assigned_role FROM user_private_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    return row[0] if row else None


def set_private_role(user_id: str, role_key: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_private_settings (user_id, assigned_role) VALUES (?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET assigned_role = ?
        """, (user_id, role_key, role_key))


# ================= 【核心修改】好感度 Getter/Setter =================

def get_user_favorability(user_id: str, role_id: str = "ling") -> int:
    """
    获取指定用户对指定角色的好感度
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # 优先查新表
        cursor.execute("SELECT favorability FROM user_role_affinity WHERE user_id = ? AND role_id = ?", (user_id, role_id))
        row = cursor.fetchone()

    if row:
        val = row[0]
    else:
        # 如果新表没数据，尝试从旧表迁移数据 (可选，这里为了简单直接给50)
        # 或者默认 50
        val = 50

    return val


def update_user_favorability(user_id: str, delta: int, role_id: str = "ling"):
    """
    更新好感度
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT favorability FROM user_role_affinity WHERE user_id = ? AND role_id = ?",
            (user_id, role_id),
        )
        row = cursor.fetchone()
        current = row[0] if row else 50
        new_fav = max(0, min(100, current + delta))

        cursor.execute("""
            INSERT INTO user_role_affinity (user_id, role_id, favorability, interaction_count) 
            VALUES (?, ?, ?, 1) 
            ON CONFLICT(user_id, role_id) DO UPDATE SET 
                favorability = ?,
                interaction_count = interaction_count + 1
        """, (user_id, role_id, new_fav, new_fav))
    return new_fav


def get_user_stats(user_id: str, role_id: str = "ling"):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT favorability, interaction_count FROM user_role_affinity WHERE user_id = ? AND role_id = ?",
                       (user_id, role_id))
        row = cursor.fetchone()
    return {"fav": row[0], "count": row[1]} if row else {"fav": 50, "count": 0}


def set_user_favorability(user_id: str, value: int, role_id: str = "ling"):
    val = max(0, min(100, value))
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_role_affinity (user_id, role_id, favorability, interaction_count) 
            VALUES (?, ?, ?, 0) 
            ON CONFLICT(user_id, role_id) DO UPDATE SET favorability = ?
        """, (user_id, role_id, val, val))
