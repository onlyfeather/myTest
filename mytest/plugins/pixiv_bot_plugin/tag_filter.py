"""屏蔽标签的匹配。纯计算，不依赖 NoneBot。

旧办法用「屏蔽词包含标签 或 标签包含屏蔽词」双向模糊匹配，屏蔽「R18」会误伤「R18G」以外的一大片，
屏蔽「成人」会误伤「成人向け」「成人の日」，冷门标签经常整页被过滤光，表现成「搜不到图」。

现在的规则：
- 标签整个相等（不分大小写、全半角、空格和连字符）就屏蔽：「R-18」「r18」「Ｒ－１８」都算同一个。
- 作品的 xRestrict 是 Pixiv 自己标的年龄分级（1 = R-18，2 = R-18G），屏蔽词里有 R-18 / R-18G 时直接按它判断，
  不依赖作者有没有打标签。
- 屏蔽词以 * 结尾时按前缀匹配，例如「猎奇*」能挡住「猎奇向」。只有用户明确要求时才模糊匹配。
- 搜索结果带的标签翻译（中文、英文）也参与比较，屏蔽「血腥」能挡住日文的「流血」。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

R18_WORDS = {"r18", "r-18", "成人"}
R18G_WORDS = {"r18g", "r-18g", "猎奇", "獵奇"}


def norm(tag: str) -> str:
    text = unicodedata.normalize("NFKC", str(tag or "")).casefold()
    return "".join(ch for ch in text if ch not in " -_・·")


def tag_names(raw: Any) -> list[str]:
    """作品的标签：搜索结果里是字符串列表，作品详情里是 {"tags": [{"tag": ..., "translation": {...}}]}。"""
    if isinstance(raw, dict):
        raw = raw.get("tags") or []
    names = []
    for item in raw or []:
        if isinstance(item, dict):
            name = item.get("tag")
            names.append(str(name)) if name else None
            for value in (item.get("translation") or {}).values():
                names.append(str(value)) if value else None
        elif item:
            names.append(str(item))
    return names


@dataclass
class TagFilter:
    blocked: set[str] = field(default_factory=set)
    _exact: set[str] = field(init=False, default_factory=set)
    _prefix: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        exact, prefix = set(), []
        for word in self.blocked:
            word = str(word).strip()
            if word.endswith("*") and len(word) > 1:
                prefix.append(norm(word[:-1]))
            elif word:
                exact.add(norm(word))
        self._exact = {w for w in exact if w}
        self._prefix = tuple(p for p in prefix if p)

    @classmethod
    def of(cls, blocked: Iterable[str]) -> TagFilter:
        return cls(set(blocked))

    def _hit(self, tag: str) -> bool:
        key = norm(tag)
        return bool(key) and (key in self._exact or any(key.startswith(p) for p in self._prefix))

    def blocks(self, illust: dict[str, Any], translations: dict[str, dict[str, str]] | None = None) -> bool:
        restrict = int(illust.get("xRestrict") or 0)
        if restrict >= 1 and self._exact & {norm(w) for w in R18_WORDS}:
            return True
        if restrict >= 2 and self._exact & {norm(w) for w in R18G_WORDS}:
            return True
        names = tag_names(illust.get("tags"))
        for name in list(names):
            for value in ((translations or {}).get(name) or {}).values():
                if value:
                    names.append(str(value))
        return any(self._hit(name) for name in names)
