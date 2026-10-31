"""多图参考：一张一张地收参考图，每收一张告诉用户这是图几。不依赖 NoneBot。

一次只处理一张：用户每条消息只取第一张图（同一条里多发的提醒他分开发），这样「图一」「图二」的顺序
就是他发的顺序，描述里可以直接说「图一的人穿着图二的衣服」。

只有 GPT Image 能真正用上多张：它的 /images/edits 接口最多收 16 张参考图，这里限制为 MAX_IMAGES 张。
NovelAI 的角色参考（Precise Reference）用多张会把几个人糅合成一个，每多一张还多扣 5 Anlas，
所以 NovelAI 只收一张。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

MAX_IMAGES = 4
DONE_WORDS = {"好了", "好", "完成", "结束", "发完了", "就这些", "可以了", "ok", "done"}
CANCEL_WORDS = {"取消", "算了", "不画了", "cancel"}
NUMERALS = "一二三四五六七八九十"


def label(index: int) -> str:
    """第 index 张（从 0 开始）叫什么：图一、图二……"""
    return f"图{NUMERALS[index]}" if index < len(NUMERALS) else f"图{index + 1}"


@dataclass
class Collected:
    images: list[bytes] = field(default_factory=list)
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)  # 过程中提醒过用户的话，测试用


# ask(提示) → 用户的回复：(文字, 图片地址列表)；超时返回 None
Ask = Callable[[str], Awaitable[tuple[str, list[str]] | None]]
Download = Callable[[str], Awaitable[bytes]]
Say = Callable[[str], Awaitable[None]]


async def collect(ask: Ask, download: Download, say: Say, *, limit: int = MAX_IMAGES,
                  first: list[bytes] | None = None, max_misses: int = 2) -> Collected:
    """一张一张收图。first：指令消息里已经带的那张（算图一）。

    - 收到一张：回「收到图N。请发送图N+1」，到上限就停
    - 发「好了」：结束（至少要一张）
    - 发「取消」或超时：放弃
    - 一条消息带了好几张：只取第一张，提醒分开发
    - 连续 max_misses 次既没图也不是「好了」：结束（有图就用已有的，没图就放弃）
    """
    out = Collected(images=list(first or [])[:limit])
    misses = 0
    # 「收到图N」和「请发送图N+1」合成一条消息发，免得一张图回两条
    prefix = f"收到{label(len(out.images) - 1)}。" if out.images else ""
    while len(out.images) < limit:
        n = len(out.images)
        text = (f"请发送{label(n)}" if n == 0
                else f"请发送{label(n)}（最多 {limit} 张），发完了回「好了」")
        reply = await ask(prefix + text + "，发「取消」放弃")
        prefix = ""
        if reply is None:
            out.cancelled = True
            return out
        words, urls = reply[0].strip().lower(), reply[1]
        if words in CANCEL_WORDS:
            out.cancelled = True
            return out
        if not urls:
            if words in DONE_WORDS and out.images:
                return out
            misses += 1
            if misses >= max_misses:
                if not out.images:
                    out.cancelled = True
                return out
            hint = "这条消息里没有图片。" + ("发完了就回「好了」" if out.images else "要先发一张图")
            out.notes.append(hint)
            prefix = hint + "。"
            continue
        misses = 0
        if len(urls) > 1:
            note = f"一条消息里有 {len(urls)} 张图，只取了第一张当{label(n)}。其余的请一张一张分开发"
            out.notes.append(note)
            await say(note)
        out.images.append(await download(urls[0]))
        prefix = f"收到{label(n)}。"
    if out.images and not out.cancelled and len(out.images) >= limit:
        await say(f"收到{label(limit - 1)}，已经 {limit} 张了，开始画")
    return out


def describe(count: int) -> str:
    """画完后附在图片下面：参考了图一到图N。"""
    if count <= 1:
        return "参考了 1 张图" if count == 1 else ""
    return f"参考了 {count} 张图（{label(0)}到{label(count - 1)}）"


def reply_parts(message: Any) -> tuple[str, list[str]]:
    """把 QQ 消息拆成（文字，图片地址）。"""
    from .._shared import qq_images

    text = message.extract_plain_text() if hasattr(message, "extract_plain_text") else str(message or "")
    return text, qq_images.image_urls(message)
