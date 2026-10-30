"""提示词：每段一个 .md 文件。开头的 <!-- --> 注释写给维护的人看（阶段、权限、来源），不发给模型。"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

DIR = Path(__file__).resolve().parent
_HEADER = re.compile(r"^\s*<!--.*?-->\s*", re.DOTALL)


@cache
def load(name: str) -> str:
    return _HEADER.sub("", (DIR / f"{name}.md").read_text(encoding="utf-8")).strip()
