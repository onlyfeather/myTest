"""剧透检查：给玩家看的文字里不能出现还没揭示的东西的关键词。不依赖 NoneBot。

不能出现的词 = 真相的关键词（到结局为止）+ 没揭示的线索的关键词 + 秘密还没说出口的 NPC 的关键词，
减去已经公开过的文字里出现过的词（开场、场景描述、已揭示线索的原文、已发生事件的描述）。
"""

from __future__ import annotations

from collections.abc import Iterable

from .module import Module


def forbidden_words(module: Module, revealed: set[str], told_npcs: set[str], public_texts: Iterable[str],
                    *, ended: bool = False) -> set[str]:
    told_words: set[str] = set()
    for n in module.npcs:
        if n.id in told_npcs:
            told_words.update(n.spoiler_words)
    words: set[str] = set()
    if not ended:
        words.update(w for w in module.spoiler_words if w not in told_words)
    for c in module.clues:
        if c.id not in revealed:
            words.update(c.spoiler_words)
    for n in module.npcs:
        if n.id not in told_npcs:
            words.update(n.spoiler_words)
    public = "\n".join(public_texts)
    return {w for w in words if w not in public}


def leaks(text: str, words: set[str], allowed: str = "") -> list[str]:
    """text 里出现的不能出现的词。allowed 里出现过的不算（比如玩家自己说出口的猜测）。"""
    return sorted(w for w in words if w in text and w not in allowed)
