"""从接口获取可选模型 / 音色列表，给 WebUI 的「获取列表」按钮用。"""

from __future__ import annotations

from typing import Any

from .http import ServiceError, describe_http_error, http_client

# 名字里带这些词的一般不是对话模型，排到后面。
_NON_CHAT_HINTS = ("embed", "rerank", "whisper", "tts", "speech", "audio", "image", "dall-e", "moderation", "vision-")
_IMAGE_HINTS = ("image", "dall-e")


async def openai_models(name: str, *, api_key: str, base_url: str, proxy: str = "", timeout: float = 30.0) -> list[str]:
    """GET {base}/models，返回模型 ID 列表（OpenAI 和大多数中转站都支持）。"""
    if not api_key.strip():
        raise ServiceError(f"请先填写并保存 {name} 的 API Key")
    async with http_client(proxy, timeout) as client:
        resp = await client.get(
            f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key.strip()}"}
        )
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(name, resp))
    try:
        data: Any = resp.json()
    except ValueError:
        raise ServiceError(f"{name} 返回的不是 JSON，检查接口地址是否填到了 /v1") from None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ServiceError(f"{name} 没有返回模型列表")
    ids = [str(m.get("id") if isinstance(m, dict) else m).strip() for m in items]
    ids = sorted({i for i in ids if i})
    if not ids:
        raise ServiceError(f"{name} 返回的模型列表是空的")
    return ids


def chat_first(ids: list[str]) -> list[tuple[str, str]]:
    def rank(model: str) -> tuple[int, str]:
        return (any(h in model.lower() for h in _NON_CHAT_HINTS), model)

    return [(m, m) for m in sorted(ids, key=rank)]


def image_only(ids: list[str]) -> list[tuple[str, str]]:
    images = [m for m in ids if any(h in m.lower() for h in _IMAGE_HINTS)]
    # 中转站的模型名可能不带 image；一个都匹配不到时返回全部。
    return [(m, m) for m in (images or ids)]


FISH_PAGE_SIZE = 20


async def fish_voices(
    *,
    api_key: str,
    base_url: str,
    query: str = "",
    page: int = 1,
    mine: bool = False,
    language: str = "",
    proxy: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """搜索 Fish Audio 音色库（mine=True 只看自己的），按使用次数排序，一页 FISH_PAGE_SIZE 个。"""
    if not api_key.strip():
        raise ServiceError("请先填写并保存 Fish Audio 的 API Key")
    from .fish_audio import api_root

    params: dict[str, Any] = {
        "page_size": FISH_PAGE_SIZE,
        "page_number": max(1, int(page)),
        "sort_by": "created_at" if mine else "task_count",
    }
    if query.strip():
        params["title"] = query.strip()
    if mine:
        params["self"] = "true"
    if language.strip():
        params["language"] = language.strip()
    async with http_client(proxy, timeout) as client:
        resp = await client.get(
            f"{api_root(base_url)}/model", params=params, headers={"Authorization": f"Bearer {api_key.strip()}"}
        )
    if resp.status_code != 200:
        raise ServiceError(describe_http_error("Fish Audio", resp))
    try:
        data = resp.json()
        raw_items = data.get("items") or []
    except (ValueError, AttributeError):
        raise ServiceError("Fish Audio 返回的音色列表格式不对") from None
    items = []
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("_id"):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        tags = [str(t) for t in item.get("tags") or [] if t][:5]
        detail = " · ".join(
            part
            for part in (
                str(author.get("nickname") or "").strip(),
                f"使用 {int(item.get('task_count') or 0)} 次",
                f"{int(item.get('like_count') or 0)} 赞",
                " ".join(f"#{t}" for t in tags),
            )
            if part
        )
        samples = [s for s in item.get("samples") or [] if isinstance(s, dict) and s.get("audio")]
        items.append(
            {
                "value": str(item["_id"]),
                "title": str(item.get("title") or "").strip() or "未命名",
                "detail": detail,
                "sample": str(samples[0]["audio"]) if samples else "",
            }
        )
    total = int(data.get("total") or 0) if isinstance(data, dict) else 0
    has_more = data.get("has_more")
    if has_more is None:
        has_more = params["page_number"] * FISH_PAGE_SIZE < total
    return {"items": items, "total": total, "has_more": bool(has_more)}
