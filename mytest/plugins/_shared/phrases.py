"""凛音的确认词：每个功能一组，随机抽一句，不连续重复。

词库默认在同目录的 phrases.json；设置 MYTEST_PHRASES_FILE 可以换成别的文件（比如放在 data 目录里，git pull 不会覆盖）。
"""

from __future__ import annotations

import json
import os
import random
import threading
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("phrases.json")
FALLBACK = {"default": ["请稍候。"]}


class PhraseBook:
    def __init__(self, path: Path, rng: random.Random | None = None):
        self.path = Path(path)
        self._rng = rng or random.Random()
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()
        self._groups = self._load()

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(FALLBACK)
        groups = {
            str(k): [str(s).strip() for s in v if str(s).strip()]
            for k, v in data.items()
            if not str(k).startswith("_") and isinstance(v, list)
        }
        return {k: v for k, v in groups.items() if v} or dict(FALLBACK)

    def reload(self) -> None:
        with self._lock:
            self._groups = self._load()

    def groups(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._groups.items()}

    def pick(self, group: str, *, name: str = "", fallback: str = "请稍候。") -> str:
        with self._lock:
            options = self._groups.get(group) or self._groups.get("default") or [fallback]
            if len(options) > 1:
                last = self._last.get(group)
                options = [o for o in options if o != last] or options
            choice = self._rng.choice(options)
            self._last[group] = choice
        return choice.replace("{name}", name or "您")


phrase_book = PhraseBook(Path(os.environ.get("MYTEST_PHRASES_FILE") or DEFAULT_FILE))


def pick(group: str, *, name: str = "") -> str:
    return phrase_book.pick(group, name=name)
