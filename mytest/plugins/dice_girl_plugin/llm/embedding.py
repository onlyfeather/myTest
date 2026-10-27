"""OpenAI 兼容的 Embedding 接口（默认硅基流动 BAAI/bge-m3）。

没配置 key、或者请求失败时返回 None，调用方退回关键词检索，不影响聊天。
"""

from __future__ import annotations

import math

import httpx
from nonebot import get_driver, logger

from ..config import config

_client: httpx.AsyncClient | None = None
# bge-m3 上限 8192 token；中文按 1 字约 1 token 保守截断。
MAX_INPUT_CHARS = 6000
BATCH_SIZE = 32


def embedding_enabled() -> bool:
    return bool(config.embedding_api_key.strip())


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(config.embedding_timeout, connect=10.0))
    return _client


@get_driver().on_shutdown
async def _close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    if not embedding_enabled() or not texts:
        return None
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [text[:MAX_INPUT_CHARS] or " " for text in texts[start : start + BATCH_SIZE]]
            resp = await _get_client().post(
                f"{config.embedding_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {config.embedding_api_key.strip()}"},
                json={"model": config.embedding_model, "input": batch, "encoding_format": "float"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[Embedding] HTTP {}: {}", resp.status_code, resp.text[:300]
                )
                return None
            data = sorted(resp.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
    except Exception as e:
        logger.warning("[Embedding] 请求失败，退回关键词检索: {}: {}", type(e).__name__, e)
        return None
    return vectors


async def embed_text(text: str) -> list[float] | None:
    result = await embed_texts([text])
    return result[0] if result else None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
