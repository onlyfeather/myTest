from __future__ import annotations

from dataclasses import dataclass

from nonebot import logger

from .._shared.services import gpt_image, model_list, novelai
from .._shared.services.http import ServiceError
from .config import draw_settings, gpt_image_settings, novelai_settings
from .prompting import DrawRequest, has_chinese, join_prompt, to_novelai_tags


@dataclass
class DrawResult:
    image: bytes
    provider: str
    prompt_used: str
    note: str = ""


async def draw_novelai(req: DrawRequest) -> DrawResult:
    s = novelai_settings
    prompt = req.prompt
    note = ""
    if s.translate and has_chinese(prompt):
        prompt = await to_novelai_tags(prompt)
        note = f"tag：{prompt}"
    width, height = novelai.SIZES[req.size]
    image, seed = await novelai.generate(
        novelai.NovelAIRequest(
            prompt=join_prompt(s.positive_prefix, prompt),
            negative=str(s.negative),
            model=str(s.model),
            width=width,
            height=height,
            steps=int(s.steps),
            scale=float(s.scale),
            sampler=str(s.sampler),
        ),
        token=str(s.token),
        base_url=str(s.base_url),
        proxy=str(draw_settings.proxy),
        timeout=float(s.timeout),
    )
    logger.info("[AI画图] NovelAI 完成：model={}, size={}, seed={}", s.model, req.size, seed)
    return DrawResult(image=image, provider="NovelAI", prompt_used=prompt, note=note)


async def draw_gpt(req: DrawRequest) -> DrawResult:
    s = gpt_image_settings
    image = await gpt_image.generate(
        req.prompt,
        api_key=str(s.api_key),
        base_url=str(s.base_url),
        model=str(s.model),
        size=gpt_image.SIZES[req.size],
        quality=str(s.quality),
        moderation=str(s.moderation),
        proxy=str(draw_settings.proxy),
        timeout=float(s.timeout),
    )
    logger.info("[AI画图] GPT Image 完成：model={}, size={}", s.model, req.size)
    return DrawResult(image=image, provider="GPT Image", prompt_used=req.prompt)


async def draw(provider: str, req: DrawRequest) -> DrawResult:
    if provider == "novelai":
        return await draw_novelai(req)
    if provider == "gpt":
        return await draw_gpt(req)
    raise ServiceError(f"未知的画图后端：{provider}")


async def list_gpt_image_models() -> list[tuple[str, str]]:
    s = gpt_image_settings
    ids = await model_list.openai_models(
        "GPT Image", api_key=str(s.api_key), base_url=str(s.base_url), proxy=str(draw_settings.proxy)
    )
    return model_list.image_only(ids)


async def test_novelai() -> str:
    s = novelai_settings
    return await novelai.check_subscription(
        token=str(s.token), base_url=str(s.account_url), proxy=str(draw_settings.proxy)
    )


async def test_gpt_image() -> str:
    s = gpt_image_settings
    return await gpt_image.check_models(
        api_key=str(s.api_key), base_url=str(s.base_url), model=str(s.model), proxy=str(draw_settings.proxy)
    )
