"""本地数据：喜好 / 屏蔽标签、关注的作者和圈名、质量阈值。只读写 SQLite，不请求 Pixiv，不依赖 NoneBot。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .pixiv_storage import PixivStorage
from .tag_filter import TagFilter

DEFAULT_MIN_QUALITY_SCORE = 50.0
DEFAULT_PREFERRED = {"风景", "插画", "原创", "美少女", "动漫", "二次元", "萌", "可爱", "唯美", "治愈", "清新", "温暖"}
DEFAULT_BLOCKED = {"R-18", "R-18G", "猎奇", "血腥", "暴力", "恐怖", "恶心", "重口"}


def normalize_alias(alias: str) -> str:
    return " ".join(str(alias).strip().casefold().split())


class TagPools:
    def __init__(self, storage: PixivStorage) -> None:
        self.storage = storage
        self.preferred: set[str] = set()
        self.blocked: set[str] = set()
        self._filter = TagFilter()

    def load(self) -> None:
        self.preferred = self.storage.load_tags("preferred")
        self.blocked = self.storage.load_tags("blocked")
        if self.storage.get_setting("preferred_tags_initialized") != "1":
            self.preferred = set(DEFAULT_PREFERRED)
            self.storage.replace_tags("preferred", self.preferred)
        if self.storage.get_setting("blocked_tags_initialized") != "1":
            self.blocked = set(DEFAULT_BLOCKED)
            self.storage.replace_tags("blocked", self.blocked)
        self._filter = TagFilter.of(self.blocked)

    @property
    def filter(self) -> TagFilter:
        return self._filter

    def _set(self, kind: str, tags: set[str]) -> None:
        self.storage.replace_tags(kind, tags)
        if kind == "blocked":
            self.blocked = set(tags)
            self._filter = TagFilter.of(self.blocked)
        else:
            self.preferred = set(tags)

    def add(self, kind: str, tag: str) -> bool:
        tag = tag.strip()
        current = self.blocked if kind == "blocked" else self.preferred
        if not tag or tag in current:
            return False
        self._set(kind, current | {tag})
        return True

    def remove(self, kind: str, tag: str) -> bool:
        tag = tag.strip()
        current = self.blocked if kind == "blocked" else self.preferred
        if tag not in current:
            return False
        self._set(kind, current - {tag})
        return True

    def replace(self, kind: str, tags: list[str]) -> None:
        self._set(kind, {t.strip() for t in tags if t and t.strip()})


class FavoriteAuthors:
    """关注的作者和圈名。改动都立刻写进 SQLite。"""

    def __init__(self, storage: PixivStorage) -> None:
        self.storage = storage
        self.authors: dict[str, dict[str, Any]] = {}
        self._alias: dict[str, str] = {}  # 规范化圈名 → 作者 ID

    def load(self) -> None:
        self.authors = self.storage.load_favorite_authors()
        self._rebuild()

    def _rebuild(self) -> None:
        self._alias = {}
        for user_id, info in self.authors.items():
            for alias in info.get("aliases", []):
                key = normalize_alias(alias)
                if key:
                    self._alias.setdefault(key, user_id)

    def _save(self) -> None:
        self._rebuild()
        try:
            self.storage.replace_favorite_authors(self.authors)
        except Exception:
            self.load()  # 写失败时恢复成数据库里的样子，内存和磁盘不能不一致
            raise

    def __contains__(self, user_id: object) -> bool:
        return str(user_id) in self.authors

    def get(self, user_id: str) -> dict[str, Any] | None:
        return self.authors.get(str(user_id))

    def resolve(self, text: str) -> str | None:
        """圈名或作者名 → 作者 ID。"""
        return self._alias.get(normalize_alias(text)) if text and text.strip() else None

    def alias_owner(self, alias: str) -> str | None:
        return self._alias.get(normalize_alias(alias))

    def aliases(self) -> dict[str, str]:
        return {alias: user_id for user_id, info in self.authors.items() for alias in info.get("aliases", [])}

    def add_many(self, authors: list[tuple[str, str]]) -> int:
        """[(作者 ID, 名字)]，已经关注的跳过。名字默认当圈名，和别人重名时不加。返回新加的数量。"""
        used = set(self._alias)
        now = datetime.now().isoformat()
        added = 0
        for user_id, name in authors:
            user_id, name = str(user_id).strip(), str(name or "").strip()
            if not user_id or user_id in self.authors:
                continue
            key = normalize_alias(name)
            self.authors[user_id] = {
                "name": name,
                "aliases": [name] if name and key not in used else [],
                "added_time": now,
                "last_check": None,
            }
            used.add(key)
            added += 1
        if added:
            self._save()
        return added

    def remove(self, user_id: str) -> bool:
        if self.authors.pop(str(user_id), None) is None:
            return False
        self._save()
        return True

    def set_aliases(self, user_id: str, aliases: list[str]) -> str | None:
        """整体替换圈名。成功返回 None，冲突时返回原因。"""
        user_id = str(user_id)
        if user_id not in self.authors:
            return f"作者 {user_id} 不在关注列表里"
        clean, seen = [], set()
        for alias in aliases:
            alias, key = str(alias).strip(), normalize_alias(alias)
            if not alias or key in seen:
                continue
            owner = self._alias.get(key)
            if owner and owner != user_id:
                return f"圈名「{alias}」已经是「{self.authors[owner].get('name') or owner}」的了"
            seen.add(key)
            clean.append(alias)
        self.authors[user_id]["aliases"] = clean
        self._save()
        return None

    def add_alias(self, user_id: str, alias: str) -> str | None:
        info = self.get(user_id)
        if info is None:
            return f"作者 {user_id} 不在关注列表里"
        if any(normalize_alias(a) == normalize_alias(alias) for a in info.get("aliases", [])):
            return f"圈名「{alias}」已经有了"
        return self.set_aliases(user_id, [*info.get("aliases", []), alias])

    def remove_alias(self, alias: str) -> str | None:
        owner = self.alias_owner(alias)
        if owner is None:
            return f"没有圈名「{alias}」"
        key = normalize_alias(alias)
        return self.set_aliases(owner, [a for a in self.authors[owner].get("aliases", []) if normalize_alias(a) != key])

    def touch(self, user_id: str) -> None:
        user_id = str(user_id)
        if user_id in self.authors:
            checked_at = datetime.now().isoformat()
            self.authors[user_id]["last_check"] = checked_at
            self.storage.update_author_last_check(user_id, checked_at)

    def summary(self) -> dict[str, Any]:
        now = datetime.now()
        recent = 0
        for info in self.authors.values():
            try:
                if (now - datetime.fromisoformat(info.get("added_time") or "")).days <= 7:
                    recent += 1
            except ValueError:
                pass
        return {
            "total_count": len(self.authors),
            "recent_added": recent,
            "never_checked": sum(1 for info in self.authors.values() if not info.get("last_check")),
            "total_aliases": sum(len(info.get("aliases", [])) for info in self.authors.values()),
            "authors": self.authors,
        }


class Library:
    def __init__(self, storage: PixivStorage) -> None:
        self.storage = storage
        self.tags = TagPools(storage)
        self.authors = FavoriteAuthors(storage)
        self.min_quality_score = DEFAULT_MIN_QUALITY_SCORE

    def load(self) -> None:
        self.tags.load()
        self.authors.load()
        raw = self.storage.get_setting("min_quality_score")
        try:
            self.min_quality_score = float(raw) if raw is not None else DEFAULT_MIN_QUALITY_SCORE
        except ValueError:
            self.min_quality_score = DEFAULT_MIN_QUALITY_SCORE

    def set_min_quality_score(self, score: int) -> int:
        score = min(max(int(score), 0), 100)
        self.storage.set_setting("min_quality_score", str(score))
        self.min_quality_score = float(score)
        return score
