import re
from dataclasses import dataclass
from typing import Optional

from ..common import PIXIV_PREFIX

MODE_MAPPING = {
    "随机": "random",
    "最新": "recent",
    "热门": "popular",
    "美图": "popular",
    "random": "random",
    "recent": "recent",
    "popular": "popular",
    "beautiful": "popular",
}


def convert_mode_to_english(mode: str) -> str:
    if not mode:
        return "random"
    return MODE_MAPPING.get(mode.lower(), "random")


@dataclass
class SearchParams:
    display_tags: str
    search_tags: str
    count: int
    mode: str
    min_bookmarks: Optional[int] = None
    illust_id: Optional[str] = None


def parse_bookmark_filter(token: str) -> Optional[int]:
    patterns = (
        r"收藏(?:数)?(?:>=|>|不少于|至少)?(\d+)",
        r"(\d+)(?:收藏|users入り|users入|users)",
        r"bookmarks?(?:>=|>)?(\d+)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, token, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_search_text(raw_text: str) -> SearchParams | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 搜图", f"{PIXIV_PREFIX} 搜图", "搜图"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break

    if not text:
        return None

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    mode = "random"
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index].lower() in MODE_MAPPING:
            mode = convert_mode_to_english(tokens.pop(index))
            break

    if len(tokens) == 1 and tokens[0].isdigit():
        return SearchParams(
            display_tags=tokens[0],
            search_tags=tokens[0],
            count=1,
            mode=mode,
            illust_id=tokens[0],
        )

    count = 1
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index].isdigit():
            count = int(tokens.pop(index))
            break

    min_bookmarks = None
    tags = []
    for token in tokens:
        bookmark_filter = parse_bookmark_filter(token)
        if bookmark_filter is not None:
            min_bookmarks = bookmark_filter
            continue
        tags.append(token)

    if not tags:
        return None

    display_tags = " ".join(tags)
    search_tags = display_tags
    if min_bookmarks is not None:
        search_tags = f"{search_tags} {min_bookmarks}users入り"

    return SearchParams(
        display_tags=display_tags,
        search_tags=search_tags,
        count=min(max(count, 1), 5),
        mode=mode,
        min_bookmarks=min_bookmarks,
    )


def parse_beautiful_text(raw_text: str) -> Optional[str]:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 美图", f"{PIXIV_PREFIX} 美图", "美图"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    tags = []
    for token in tokens:
        if token.isdigit() or token.lower() in MODE_MAPPING:
            continue
        if parse_bookmark_filter(token) is not None:
            continue
        tags.append(token)

    return " ".join(tags) if tags else None


@dataclass
class LatestParams:
    tags: str
    count: int = 1
    hours: int = 24


def parse_latest_text(raw_text: str) -> LatestParams | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 最新", f"{PIXIV_PREFIX} 最新", "最新"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    numbers = []
    tags = []
    for token in tokens:
        if token.isdigit():
            numbers.append(int(token))
        else:
            tags.append(token)

    if not tags:
        return None

    count = numbers[0] if len(numbers) >= 1 else 1
    hours = numbers[1] if len(numbers) >= 2 else 24
    return LatestParams(
        tags=" ".join(tags),
        count=min(max(count, 1), 10),
        hours=min(max(hours, 1), 168),
    )


def parse_hot_text(raw_text: str) -> tuple[str, int] | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 热门", f"{PIXIV_PREFIX} 热门", "热门"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    count = 1
    tags = []
    for token in tokens:
        if token.isdigit():
            count = int(token)
        else:
            tags.append(token)

    if not tags:
        return None

    return " ".join(tags), min(max(count, 1), 5)
