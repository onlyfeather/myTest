"""凛音推荐歌：LLM 按要求选歌名，再到平台搜索核对，搜不到的（编出来的）丢掉。

参考的优先级写进提示词（DESIGN.md 第 11 节，用户确认过）：
1. 提问人的要求是硬性条件
2. 要求没限定的部分才参考：群里最近的聊天、本群一起听放过的歌、提问人自己的歌单
3. 冲突时按要求来；不推本群最近放过的歌
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nonebot import logger

from .._shared.services import llm
from .._shared.services.http import ServiceError
from .sources.base import PLATFORMS, SourceError, Track
from .sources.resolver import Sources, artist_set, normalize

MAX_ATTEMPTS = 3
MAX_COUNT = 5

SYSTEM_PROMPT = """你是选歌助手，替一个叫白鸟凛音的角色给群友推荐歌。只负责选歌，不负责说话。

输出格式：只输出一个 JSON 对象，形如
{"songs": [{"title": "晴天", "artist": "周杰伦"}, {"title": "Lemon", "artist": "米津玄師"}]}
不要输出 JSON 以外的任何文字，不要用代码块包起来。

选歌规则，按优先级：
1. 「要求」是硬性条件：曲风、心情、歌手、语言、年代、场景、「像某首歌那样」，都必须满足。
2. 要求里没限定的部分，才参考「群里最近在聊」「最近一起听放过」「提问人的歌单」，用来贴合气氛和口味。
   这三样只是参考，不要喧宾夺主；和要求冲突时一律按要求来。
3. 不要选「最近一起听放过」里的歌。
4. 只选真实存在、能在 QQ 音乐 / 网易云 / 酷狗搜到的歌。歌名和歌手用原文（日文歌写日文，英文歌写英文）。
   不确定是否存在的不要写。
5. 歌手写主唱，有多位时只写第一位。不要写翻唱版、Live 版，除非要求里明确要。"""


@dataclass
class RecommendRequest:
    want: str  # 提问人的要求，例如「来首安静的」「周杰伦的老歌」
    count: int = 1
    chat: list[str] = field(default_factory=list)  # 群里最近的聊天（MaiBot 带过来）
    played: list[str] = field(default_factory=list)  # 本群一起听最近放过的「歌名 - 歌手」
    taste: list[str] = field(default_factory=list)  # 提问人自己的喜欢的音乐和歌单


def build_prompt(req: RecommendRequest, n: int) -> str:
    parts = [f"要求：{req.want.strip() or '随便来点好听的'}", f"选 {n} 首。"]
    if req.chat:
        parts.append("群里最近在聊（仅供参考气氛）：\n" + "\n".join(f"- {line[:120]}" for line in req.chat[-20:]))
    if req.played:
        parts.append("最近一起听放过（不要选这些）：\n" + "\n".join(f"- {s}" for s in req.played[-50:]))
    if req.taste:
        parts.append("提问人的歌单（仅供参考口味）：\n" + "\n".join(f"- {s}" for s in req.taste[:30]))
    return "\n\n".join(parts)


def parse_songs(reply: str) -> list[tuple[str, str]] | None:
    text = llm.THINK_BLOCK.sub("", reply or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    songs = data.get("songs") if isinstance(data, dict) else None
    if not isinstance(songs, list):
        return None
    out = []
    for item in songs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:80]
        artist = str(item.get("artist") or "").strip()[:60]
        if title:
            out.append((title, artist))
    return out or None


def _title_matches(want: str, got: str) -> bool:
    a, b = normalize(want), normalize(got)
    if not a or not b:
        return False
    if a == b:
        return True
    # 平台歌名常带后缀：「Lemon」→「Lemon (Original)」；但「晴天」不能匹配「晴天 (Live)」以外的别的歌
    stripped = re.sub(r"\(.*?\)|\[.*?\]", "", got)
    return normalize(stripped) == a


def pick_match(title: str, artist: str, results: list[Track]) -> Track | None:
    """搜索结果里挑出歌名一致、歌手对得上的那首。对不上返回 None（多半是 LLM 编的）。"""
    wanted_artists = artist_set(artist)
    for track in results:
        if not _title_matches(title, track.title):
            continue
        if wanted_artists and not (wanted_artists & artist_set(track.artists)):
            continue
        if re.search(r"live|伴奏|instrumental|karaoke|カラオケ|翻自|cover", track.title, re.IGNORECASE) and not re.search(
            r"live|伴奏|instrumental|karaoke|カラオケ|翻自|cover", title, re.IGNORECASE
        ):
            continue
        return track
    return None


Chat = Callable[[list[dict[str, str]]], Awaitable[str]]


async def _default_chat(messages: list[dict[str, str]]) -> str:
    return await llm.chat(messages, temperature=0.8)


async def recommend(
    req: RecommendRequest,
    sources: Sources,
    *,
    playable: Callable[[Track], bool],
    chat: Chat = _default_chat,
    platforms: tuple[str, ...] = PLATFORMS,
) -> list[Track]:
    """返回核对过的歌，最多 count 首。LLM 每次多选一倍，核对不过的丢掉，不够就再问一次，最多 3 次。"""
    count = max(1, min(MAX_COUNT, req.count))
    found: list[Track] = []
    seen: set[str] = set()
    rejected: list[str] = []
    played = {normalize(s.split(" - ")[0]) for s in req.played}
    order = [p for p in platforms if p in sources and sources.available(p)]
    if not order:
        raise ServiceError("音乐平台暂时都不可用")
    last_problem = ""
    for _ in range(MAX_ATTEMPTS):
        need = count - len(found)
        if need <= 0:
            break
        prompt = build_prompt(req, need * 2)
        if rejected:
            prompt += "\n\n这些搜不到或者不符合，换别的：\n" + "\n".join(f"- {s}" for s in rejected[-20:])
        try:
            reply = await chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}])
        except llm.EmptyReplyError as e:
            if not e.retryable:
                raise
            last_problem = str(e)
            continue
        songs = parse_songs(reply)
        if songs is None:
            last_problem = "选歌结果格式不对"
            logger.info("[一起听] 推荐：格式不对，重试。原文：{}", (reply or "")[:200])
            continue
        for title, artist in songs:
            key = normalize(title) + "|" + normalize(artist)
            if key in seen:
                continue
            seen.add(key)
            if normalize(title) in played:
                rejected.append(f"{title} - {artist}（最近放过）")
                continue
            track = await _verify(title, artist, sources, order, playable)
            if track is None:
                rejected.append(f"{title} - {artist}")
                continue
            found.append(track)
            if len(found) >= count:
                break
    if not found and last_problem:
        raise ServiceError(last_problem)
    return found


async def _verify(title: str, artist: str, sources: Sources, order: list[str],
                  playable: Callable[[Track], bool]) -> Track | None:
    """按平台顺序搜，第一首对得上而且放得了的就用它。只对得上但放不了的（会员歌）留作备选。"""
    fallback: Track | None = None
    for platform in order:
        try:
            results = await sources.search(platform, f"{title} {artist}".strip(), 10)
        except SourceError:
            continue
        match = pick_match(title, artist, results)
        if match is None:
            continue
        if playable(match):
            return match
        fallback = fallback or match
    return fallback
