"""模组：格式、读入校验、自带的模组。"""

from __future__ import annotations

from pathlib import Path

from .loader import LoadResult, Problem, load_module_data, load_module_text
from .model import Module

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "modules"


def builtin_modules() -> dict[str, LoadResult]:
    """插件自带的模组，文件名 → 校验结果。"""
    return {p.name: load_module_text(p.read_text(encoding="utf-8")) for p in sorted(BUILTIN_DIR.glob("*.json"))}


__all__ = ["BUILTIN_DIR", "LoadResult", "Module", "Problem", "builtin_modules", "load_module_data", "load_module_text"]
