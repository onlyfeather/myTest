"""交互式点歌：搜索结果编号列表、等对方回复编号。纯逻辑，不依赖 NoneBot。

每个人在每个会话（群或私聊）里同时只有一份待选列表，2 分钟内回复编号有效，选过或者重新搜就作废。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .sources.base import PLATFORM_NAMES, Track

PICK_TTL = 120.0
MAX_PENDING = 500
_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")


@dataclass(frozen=True)
class Choice:
    number: int
    track: Track


@dataclass
class _Pending:
    choices: list[Choice]
    expires: float


def number_choices(groups: list[tuple[str, list[Track]]]) -> list[Choice]:
    """按平台顺序连续编号：QQ 音乐 1–3、网易云 4–6、酷狗 7–9。"""
    choices: list[Choice] = []
    for _, tracks in groups:
        for track in tracks:
            choices.append(Choice(len(choices) + 1, track))
    return choices


def _duration(ms: int) -> str:
    if ms <= 0:
        return ""
    s = ms // 1000
    return f" {s // 60}:{s % 60:02d}"


def format_list(keyword: str, groups: list[tuple[str, list[Track]]], errors: dict[str, str],
                choices: list[Choice], *, vip_ready: Callable[[str], bool]) -> str:
    """搜索结果的文字列表。会员歌标出来，bot 没配会员账号的平台写明「试听」。"""
    by_ref = {c.track.ref: c.number for c in choices}
    lines = [f"🎵「{keyword}」的搜索结果"]
    for platform, tracks in groups:
        name = PLATFORM_NAMES.get(platform, platform)
        if errors.get(platform):
            lines.append(f"【{name}】{errors[platform]}")
            continue
        if not tracks:
            lines.append(f"【{name}】没搜到")
            continue
        lines.append(f"【{name}】")
        for track in tracks:
            tag = ""
            if track.vip:
                tag = " [会员]" if vip_ready(platform) else " [会员·只能试听]"
            lines.append(f"{by_ref[track.ref]}. {track.title} - {track.artists}{_duration(track.duration_ms)}{tag}")
    lines.append(f"回复编号发歌（{int(PICK_TTL // 60)} 分钟内有效），回复 0 取消")
    return "\n".join(lines)


class Picker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._pending: dict[tuple[str, str], _Pending] = {}

    def _purge(self) -> None:
        now = self._clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}

    def offer(self, scope: str, user_id: str, choices: list[Choice]) -> None:
        self._purge()
        if len(self._pending) >= MAX_PENDING:
            # 太多人同时在选：丢掉最早的
            self._pending.pop(next(iter(self._pending)))
        self._pending[(scope, user_id)] = _Pending(choices, self._clock() + PICK_TTL)

    def waiting(self, scope: str, user_id: str) -> bool:
        pending = self._pending.get((scope, user_id))
        return pending is not None and pending.expires > self._clock()

    def take(self, scope: str, user_id: str, text: str) -> Track | str | None:
        """对方回复了一条消息。

        - 不是在等这个人选歌、或者回复的不是纯数字：返回 None，消息交给别的功能处理；
        - 回复 0：取消，返回提示文字；
        - 编号超出范围：返回提示文字，列表保留，可以再选；
        - 选中：返回那首歌，列表作废。
        """
        key = (scope, user_id)
        pending = self._pending.get(key)
        if pending is None or pending.expires <= self._clock():
            self._pending.pop(key, None)
            return None
        m = _NUMBER.match(text or "")
        if not m:
            return None
        number = int(m.group(1))
        if number == 0:
            del self._pending[key]
            return "好，不点了。"
        for choice in pending.choices:
            if choice.number == number:
                del self._pending[key]
                return choice.track
        return f"没有 {number} 号，回复 1–{len(pending.choices)} 之间的编号，或者回复 0 取消。"

    def cancel(self, scope: str, user_id: str) -> None:
        self._pending.pop((scope, user_id), None)
