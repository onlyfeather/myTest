"""掷骰表达式：3d6+2、d20、2d6+1d4-1。不依赖 NoneBot。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

MAX_DICE = 100  # 一次最多掷多少个骰子
MAX_FACES = 1000
MAX_TERMS = 10
MAX_CONSTANT = 10000

_TERM = re.compile(r"([+-])?(?:(\d*)d(\d+)|(\d+))", re.IGNORECASE)


class DiceError(ValueError):
    """表达式不合法。message 直接显示给用户。"""


@dataclass(frozen=True)
class DiceRoll:
    expression: str  # 规范写法，例如 3D6+2
    total: int
    detail: str  # 例如 3D6+2=(4+2+6)+2=14
    minimum: int
    maximum: int


def roll(expression: str, rng: random.Random | None = None, *, require_dice: bool = True) -> DiceRoll:
    """require_dice=False 时允许纯常数（理智检定的损失可以写 0、1）。"""
    rng = rng or random.Random()
    text = expression.replace(" ", "")
    if not text:
        raise DiceError("表达式是空的")
    terms: list[tuple[int, int, int]] = []  # (符号, 个数, 面数)；面数为 0 表示常数
    pos = 0
    while pos < len(text):
        m = _TERM.match(text, pos)
        if not m or m.end() == pos or (terms and not m.group(1)):
            raise DiceError(f"看不懂「{expression}」，例：3d6+2、d20、2d6+1d4")
        sign = -1 if m.group(1) == "-" else 1
        if m.group(3) is not None:
            count = int(m.group(2) or 1)
            faces = int(m.group(3))
            if not 1 <= count <= MAX_DICE:
                raise DiceError(f"一次最多掷 {MAX_DICE} 个骰子")
            if not 2 <= faces <= MAX_FACES:
                raise DiceError(f"骰子面数要在 2 到 {MAX_FACES} 之间")
            terms.append((sign, count, faces))
        else:
            value = int(m.group(4))
            if value > MAX_CONSTANT:
                raise DiceError(f"常数不能超过 {MAX_CONSTANT}")
            terms.append((sign, value, 0))
        pos = m.end()
    if len(terms) > MAX_TERMS:
        raise DiceError(f"最多 {MAX_TERMS} 项")
    if sum(count for _, count, faces in terms if faces) > MAX_DICE:
        raise DiceError(f"一次最多掷 {MAX_DICE} 个骰子")
    if require_dice and not any(faces for _, _, faces in terms):
        raise DiceError("表达式里至少要有一个骰子，例：1d20+5")

    total = low = high = 0
    names, shown = [], []
    for i, (sign, count, faces) in enumerate(terms):
        prefix = ("-" if sign < 0 else "+") if i else ("-" if sign < 0 else "")
        if faces:
            values = [rng.randint(1, faces) for _ in range(count)]
            names.append(f"{prefix}{count}D{faces}")
            shown.append(prefix + (str(values[0]) if count == 1 else "(" + "+".join(map(str, values)) + ")"))
            subtotal, sub_low, sub_high = sum(values), count, count * faces
        else:
            names.append(f"{prefix}{count}")
            shown.append(f"{prefix}{count}")
            subtotal = sub_low = sub_high = count
        total += sign * subtotal
        low += sign * (sub_low if sign > 0 else sub_high)
        high += sign * (sub_high if sign > 0 else sub_low)

    name = "".join(names)
    steps = "".join(shown)
    if name == str(total):
        detail = name
    elif steps == str(total):
        detail = f"{name}={total}"
    else:
        detail = f"{name}={steps}={total}"
    return DiceRoll(expression=name, total=total, detail=detail, minimum=low, maximum=high)
