"""WebUI「Pixiv」里的关注作者列表：添加、取关、圈名、从 Pixiv 账号导入。不依赖 NoneBot。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

MAX_IMPORT = 2000
IMPORT_PAGE = 100
IMPORT_GAP = 1.0
MAX_ADD_AT_ONCE = 50
_USER_URL = re.compile(r"pixiv\.net/(?:\w+/)?(?:users/|member\.php\?id=)(\d+)")
_ID = re.compile(r"^\d{1,12}$")


def parse_author_ids(lines: list[str]) -> tuple[list[str], list[str]]:
    """每行一个作者 ID 或主页链接。返回 (ID 列表, 看不懂的行)。"""
    ids, bad = [], []
    for line in lines:
        text = line.strip()
        m = _USER_URL.search(text)
        if m:
            text = m.group(1)
        if _ID.match(text):
            if text not in ids:
                ids.append(text)
        elif text:
            bad.append(line.strip()[:40])
    return ids, bad


def _date(text: str | None) -> str:
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def list_items(spider) -> dict[str, Any]:
    seen = spider.storage.all_author_seen()
    items = []
    for user_id, info in spider.favorite_authors.items():
        aliases = [a for a in info.get("aliases", []) if a != info.get("name")]
        parts = [f"ID {user_id}"]
        if aliases:
            parts.append("圈名：" + "、".join(aliases))
        if added := _date(info.get("added_time")):
            parts.append(f"{added} 关注")
        badges = ["推送已就绪" if user_id in seen else "还没检查过"]
        items.append({
            "id": user_id,
            "title": info.get("name") or f"作者 {user_id}",
            "detail": "，".join(parts),
            "link": f"https://www.pixiv.net/users/{user_id}",
            "badges": badges,
            "values": {"aliases": info.get("aliases", [])},
        })
    notice = "" if spider.is_logged_in else "还没有填登录 Cookie（在上面）：添加作者时查不到名字，导入和推送也用不了"
    return {"items": items, "notice": notice}


async def add_authors(spider, lines: list[str]) -> str:
    ids, bad = parse_author_ids(lines)
    if not ids:
        raise ValueError("没有认出作者 ID。每行一个数字 ID，或者作者主页链接（https://www.pixiv.net/users/123）")
    if len(ids) > MAX_ADD_AT_ONCE:
        raise ValueError(f"一次最多添加 {MAX_ADD_AT_ONCE} 个；更多的请用「从我的 Pixiv 关注导入」")
    fresh = [i for i in ids if i not in spider.favorite_authors]
    found, missing = [], []
    for user_id in fresh:
        info = await spider.get_user_info(user_id) if spider.is_logged_in else None
        body = (info or {}).get("body") or {}
        if info and not info.get("error") and body.get("name"):
            found.append((user_id, body["name"]))
        elif spider.is_logged_in:
            missing.append(user_id)
        else:
            found.append((user_id, ""))
    added = spider.add_favorite_authors_bulk(found)
    parts = [f"添加了 {added} 个作者"]
    if len(ids) > len(fresh):
        parts.append(f"{len(ids) - len(fresh)} 个本来就关注了")
    if missing:
        parts.append("这些 ID 在 Pixiv 上找不到：" + "、".join(missing))
    if bad:
        parts.append("这几行看不懂：" + "、".join(bad[:5]))
    return "，".join(parts)


async def import_following(spider, *, include_private: bool = True) -> str:
    """把 Cookie 对应的 Pixiv 账号关注的作者全部加进来。"""
    if not spider.is_logged_in:
        raise ValueError("还没有填登录 Cookie，在上面的「登录 Cookie」里填好并保存")
    me = spider.user_id
    if not me:
        info = await spider.get_user_info()
        me = str(((info or {}).get("body") or {}).get("userId") or "")
    if not me:
        raise ValueError("读不到 Cookie 对应的 Pixiv 账号，Cookie 可能过期了。点右上角「测试连接」看看")
    users: dict[str, str] = {}
    for rest in ("show", "hide") if include_private else ("show",):
        offset = 0
        while len(users) < MAX_IMPORT:
            body = await spider.get_following(me, offset=offset, limit=IMPORT_PAGE, rest=rest)
            if body is None:
                if not users:
                    raise ValueError("读取 Pixiv 关注列表失败，Cookie 可能过期了，或者 Pixiv 暂时连不上")
                break
            page = body.get("users") or []
            for user in page:
                if user.get("userId"):
                    users.setdefault(str(user["userId"]), str(user.get("userName") or ""))
            offset += len(page)
            if not page or offset >= int(body.get("total") or 0):
                break
            await asyncio.sleep(IMPORT_GAP)
    added = spider.add_favorite_authors_bulk(list(users.items())[:MAX_IMPORT])
    text = f"Pixiv 上关注了 {len(users)} 个作者，新加了 {added} 个"
    if len(users) >= MAX_IMPORT:
        text += f"（最多导入 {MAX_IMPORT} 个）"
    return text


async def run_action(spider, action: str, item_id: str, value: Any) -> str:
    if action == "add":
        return await add_authors(spider, value or [])
    if action == "import":
        return await import_following(spider)
    if item_id not in spider.favorite_authors:
        raise ValueError("这个作者已经不在列表里了，刷新一下")
    name = spider.favorite_authors[item_id].get("name") or item_id
    if action == "remove":
        if not spider.remove_favorite_author(item_id):
            raise ValueError("取关失败，详情见日志")
        spider.storage.delete_author_seen(item_id)
        return f"已取关「{name}」"
    if action == "aliases":
        if reason := spider.set_author_aliases(item_id, value or []):
            raise ValueError(reason)
        return f"已更新「{name}」的圈名"
    if action == "reset_push":
        spider.storage.delete_author_seen(item_id)
        return f"「{name}」下次检查时会重新记录最新作品，之后的新作品才推送"
    raise KeyError(action)
