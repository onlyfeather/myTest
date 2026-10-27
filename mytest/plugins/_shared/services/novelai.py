"""NovelAI 文生图。接口：POST {base}/ai/generate-image，返回 ZIP（里面一张 PNG）。"""

from __future__ import annotations

import base64
import io
import random
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx

from .http import ServiceError, describe_http_error, http_client

NAME = "NovelAI"

MEDIUM_MODEL = "nai-diffusion-5-full-medium"

MODELS = (
    ("nai-diffusion-5-full", "V5 Full"),
    (MEDIUM_MODEL, "V5 Full 节约模式（14 步，更省点数）"),
    ("nai-diffusion-5-curated", "V5 Curated"),
    ("nai-diffusion-4-5-full", "V4.5 Full"),
    ("nai-diffusion-4-5-curated", "V4.5 Curated"),
    ("nai-diffusion-4-full", "V4 Full"),
    ("nai-diffusion-3", "Anime V3"),
)

# 节约模式（Medium effort）是独立的蒸馏模型，只能 14 步 + Euler Ancestral，
# 不支持自定义负面提示词（官方固定预设）。
MEDIUM_STEPS = 14
MEDIUM_SAMPLER = "k_euler_ancestral"

SAMPLERS = (
    ("k_euler_ancestral", "Euler Ancestral"),
    ("k_euler", "Euler"),
    ("k_dpmpp_2s_ancestral", "DPM++ 2S Ancestral"),
    ("k_dpmpp_2m_sde", "DPM++ 2M SDE"),
    ("k_dpmpp_2m", "DPM++ 2M"),
)

SIZES = {
    "竖图": (832, 1216),
    "横图": (1216, 832),
    "方图": (1024, 1024),
}


@dataclass
class CharacterReference:
    """角色参考（Precise Reference 里的 Character Reference）：让生成的角色长得像参考图。

    NovelAI 的另一种用图方式是 Vibe Transfer（氛围参考，学画面的整体感觉），这里不用。
    """

    image: bytes  # 已经补边到 REFERENCE_CANVASES 之一的 PNG
    with_style: bool = False  # True 时连画风一起参考（character&style）
    strength: float = 1.0
    fidelity: float = 1.0


# 角色参考只接受这三种尺寸，其他尺寸的图要等比缩放后补黑边。
REFERENCE_CANVASES = ((1024, 1536), (1536, 1024), (1472, 1472))
# 官方文档：Precise Reference 只在 V4.5 模型上可用。
REFERENCE_MODELS = ("nai-diffusion-4-5-full", "nai-diffusion-4-5-curated")


def supports_reference(model: str) -> bool:
    return model in REFERENCE_MODELS


@dataclass
class NovelAIRequest:
    prompt: str
    negative: str
    model: str
    width: int
    height: int
    steps: int
    scale: float
    sampler: str
    seed: int | None = None
    reference: CharacterReference | None = None


OFFICIAL_HOST = "https://image.novelai.net"
# 旧地址只留给官方网页用，第三方请求会返回 400
# 「Please refresh NovelAI.net. If using a third-party tool, update to the image URL.」
LEGACY_HOST = "api.novelai.net"


def api_root(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url:
        return OFFICIAL_HOST
    try:
        host = httpx.URL(url).host
    except httpx.InvalidURL:
        return url
    return OFFICIAL_HOST if host == LEGACY_HOST else url


def is_v5(model: str) -> bool:
    return model.startswith("nai-diffusion-5")


def build_payload(req: NovelAIRequest) -> dict[str, Any]:
    seed = req.seed if req.seed is not None else random.randint(0, 2**32 - 1)
    medium = req.model == MEDIUM_MODEL
    negative = "" if medium else req.negative
    parameters: dict[str, Any] = {
        "params_version": 4 if is_v5(req.model) else 3,
        "width": req.width,
        "height": req.height,
        "scale": req.scale,
        "sampler": MEDIUM_SAMPLER if medium else req.sampler,
        "steps": MEDIUM_STEPS if medium else req.steps,
        "seed": seed,
        "n_samples": 1,
        "noise_schedule": "karras",
        "uc": negative,
        "qualityToggle": False,
        "legacy": False,
        "legacy_v3_extend": False,
    }
    if medium:
        parameters["cfg_rescale"] = 0
    if req.model.startswith(("nai-diffusion-4", "nai-diffusion-5")):
        # V4 / V4.5 / V5 必须带结构化提示词（V5 字段名仍是 v4_prompt），没有角色时 char_captions 传空数组。
        req = NovelAIRequest(**{**req.__dict__, "negative": negative})
        parameters.update(
            {
                "legacy_uc": False,
                "v4_prompt": {
                    "caption": {"base_caption": req.prompt, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {"base_caption": req.negative, "char_captions": []},
                    "legacy_uc": False,
                },
            }
        )
    else:
        parameters.update({"sm": False, "sm_dyn": False})
    if req.reference is not None:
        if not supports_reference(req.model):
            raise ServiceError("角色参考图只支持 V4.5 模型（V4.5 Full / V4.5 Curated），请在 WebUI 里换模型")
        ref = req.reference
        parameters.update(
            {
                "director_reference_images": [base64.b64encode(ref.image).decode()],
                "director_reference_descriptions": [
                    {
                        "caption": {
                            "base_caption": "character&style" if ref.with_style else "character",
                            "char_captions": [],
                        },
                        "legacy_uc": False,
                    }
                ],
                "director_reference_information_extracted": [1],
                "director_reference_strength_values": [ref.strength],
                # 网页上的 Fidelity 滑块：接口里传的是 1 - fidelity。
                "director_reference_secondary_strength_values": [round(1 - ref.fidelity, 4)],
            }
        )
    return {"input": req.prompt, "model": req.model, "action": "generate", "parameters": parameters}


def extract_image(content: bytes) -> bytes:
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise ServiceError(f"{NAME} 返回的压缩包是空的")
            return zf.read(names[0])
    except zipfile.BadZipFile:
        raise ServiceError(f"{NAME} 返回的不是图片") from None


async def generate(
    req: NovelAIRequest,
    *,
    token: str,
    base_url: str,
    proxy: str = "",
    timeout: float = 120.0,
) -> tuple[bytes, int]:
    """返回 (PNG 字节, 实际使用的种子)。"""
    if not token.strip():
        raise ServiceError("还没有配置 NovelAI 的 Token")
    payload = build_payload(req)
    url = f"{api_root(base_url)}/ai/generate-image"
    headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/x-zip-compressed"}
    async with http_client(proxy, timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code not in {200, 201}:
        raise ServiceError(describe_http_error(NAME, resp))
    return extract_image(resp.content), payload["parameters"]["seed"]


async def check_subscription(*, token: str, base_url: str, proxy: str = "", timeout: float = 30.0) -> str:
    """调用订阅信息接口，确认 Token 可用，并返回剩余点数。"""
    if not token.strip():
        raise ServiceError("还没有配置 NovelAI 的 Token")
    # 订阅接口和画图接口在同一个地址（image.novelai.net）；中转站可能没有这个接口。
    url = f"{api_root(base_url)}/user/subscription"
    async with http_client(proxy, timeout) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token.strip()}"})
    if resp.status_code != 200:
        raise ServiceError(describe_http_error(NAME, resp))
    data = resp.json()
    steps = data.get("trainingStepsLeft") or {}
    anlas = 0
    if isinstance(steps, dict):
        anlas = int(steps.get("fixedTrainingStepsLeft") or 0) + int(steps.get("purchasedTrainingSteps") or 0)
    tier = {0: "免费", 1: "Tablet", 2: "Scroll", 3: "Opus"}.get(int(data.get("tier") or 0), "未知")
    return f"Token 可用，订阅：{tier}，剩余 Anlas：{anlas}"
