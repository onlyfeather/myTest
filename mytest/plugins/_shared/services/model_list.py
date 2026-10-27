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


async def fish_voices(*, api_key: str, base_url: str, proxy: str = "", timeout: float = 30.0) -> list[tuple[str, str]]:
    """自己的音色在前，然后是公共音色库里最常用的。"""
    if not api_key.strip():
        raise ServiceError("请先填写并保存 Fish Audio 的 API Key")
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    url = f"{base_url.rstrip('/')}/model"
    result: list[tuple[str, str]] = []
    async with http_client(proxy, timeout) as client:
        for params, tag in (
            ({"self": "true", "page_size": 100, "sort_by": "created_at"}, "我的"),
            ({"page_size": 100, "sort_by": "task_count", "language": "zh"}, "热门"),
        ):
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                raise ServiceError(describe_http_error("Fish Audio", resp))
            try:
                items = resp.json().get("items") or []
            except (ValueError, AttributeError):
                raise ServiceError("Fish Audio 返回的音色列表格式不对") from None
            for item in items:
                if not isinstance(item, dict) or not item.get("_id"):
                    continue
                title = str(item.get("title") or "").strip() or "未命名"
                result.append((str(item["_id"]), f"{tag} · {title}"))
    if not result:
        raise ServiceError("没有找到可用的音色")
    return result
