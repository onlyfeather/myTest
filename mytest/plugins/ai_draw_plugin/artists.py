"""每个人自己的 NovelAI 画师串。没有设置的人用 WebUI 里配置的默认画师串。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

MAX_LENGTH = 1000
CLEAR_WORDS = {"默认", "清除", "清空", "重置", "default", "reset", "clear"}


class ArtistStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def get(self, user_id: str) -> str:
        item = self._data.get(str(user_id))
        return str(item.get("artists") or "") if item else ""

    def set(self, user_id: str, artists: str) -> str:
        artists = clean(artists)
        if not artists:
            raise ValueError("画师串是空的")
        if len(artists) > MAX_LENGTH:
            raise ValueError(f"画师串太长了，最多 {MAX_LENGTH} 个字符")
        with self._lock:
            data = {**self._data, str(user_id): {"artists": artists, "updated_at": time.time()}}
            self._save(data)
            self._data = data
        return artists

    def clear(self, user_id: str) -> bool:
        with self._lock:
            if str(user_id) not in self._data:
                return False
            data = {k: v for k, v in self._data.items() if k != str(user_id)}
            self._save(data)
            self._data = data
        return True

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise RuntimeError(f"画师串文件 {self.path} 无法读取: {e}") from e
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".artists-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def clean(text: str) -> str:
    """统一逗号、去掉首尾的逗号和空白，保留 NovelAI 的权重语法（{}、[]、1.2::xx::）。"""
    text = text.replace("，", ",").replace("\n", ",").strip().strip(",").strip()
    parts = [p.strip() for p in text.split(",")]
    return ", ".join(p for p in parts if p)


def is_clear_word(text: str) -> bool:
    return text.strip().lower() in CLEAR_WORDS


def compose(prefix: str, artists: str, prompt: str) -> str:
    """质量词, 画师串, 描述。任何一段为空就跳过。"""
    parts = [p.strip().strip(",").strip() for p in (prefix, artists, prompt)]
    return ", ".join(p for p in parts if p)
