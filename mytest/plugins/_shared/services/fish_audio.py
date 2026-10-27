"""Fish Audio 语音合成：POST {base}/v1/tts，模型通过 model 请求头选择，返回音频字节。"""

from __future__ import annotations

from typing import Any

from .http import ServiceError, describe_http_error, http_client

NAME = "Fish Audio"

DRAMA_MODEL = "drama-3"

MODELS = (
    ("s2.1-pro", "S2.1 Pro"),
    (DRAMA_MODEL, "Drama 3（可用 [语气描述] 指导演绎）"),
    ("s2.1-pro-free", "S2.1 Pro Free（免费开发者额度）"),
    ("s2-pro", "S2 Pro"),
)

FORMATS = (
    ("mp3", "MP3"),
    ("wav", "WAV"),
)


def build_payload(
    text: str, *, reference_id: str, audio_format: str, speed: float, volume: float
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "format": audio_format,
        "normalize": True,
        "latency": "normal",
        "prosody": {"speed": speed, "volume": volume},
    }
    if reference_id.strip():
        payload["reference_id"] = reference_id.strip()
    if audio_format == "mp3":
        payload["mp3_bitrate"] = 128
    return payload


async def synthesize(
    text: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    reference_id: str,
    audio_format: str = "mp3",
    speed: float = 1.0,
    volume: float = 0.0,
    proxy: str = "",
    timeout: float = 120.0,
) -> bytes:
    if not api_key.strip():
        raise ServiceError("还没有配置 Fish Audio 的 API Key")
    if not text.strip():
        raise ServiceError("没有要合成的文字")
    payload = build_payload(text, reference_id=reference_id, audio_format=audio_format, speed=speed, volume=volume)
    headers = {"Authorization": f"Bearer {api_key.strip()}", "model": model}
    async with http_client(proxy, timeout) as client:
        resp = await client.post(f"{base_url.rstrip('/')}/v1/tts", json=payload, headers=headers)
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(NAME, resp))
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type or not resp.content:
        raise ServiceError(f"{NAME} 没有返回音频")
    return resp.content
