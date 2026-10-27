from __future__ import annotations

from dataclasses import dataclass, field

from nonebot import logger

from .._shared.services import gpt_image, images, model_list, novelai
from .._shared.services.http import ServiceError
from .artists import compose
from .config import draw_settings, gpt_image_settings, novelai_settings
from .prompting import DrawRequest, has_chinese, to_novelai_tags


@dataclass
class DrawResult:
    image: bytes
    provider: str
    prompt_used: str
    note: str = ""


@dataclass
class DrawInputs:
    """一次画图除了描述之外的输入。都可以为空，为空就用默认。"""

    references: list[bytes] = field(default_factory=list)  # 原始图片字节
    artists: str | None = None  # None 表示用默认画师串


def check_reference_supported(provider: str) -> str:
    """当前配置能否用参考图；不能时返回原因，能用返回空字符串。"""
    if provider == "novelai" and not novelai.supports_reference(str(novelai_settings.model)):
        return "NovelAI 的角色参考图只支持 V4.5 模型，当前模型不支持。请管理员在 WebUI 里把模型换成 V4.5 Full 或 V4.5 Curated"
    return ""


async def draw_novelai(req: DrawRequest, inputs: DrawInputs) -> DrawResult:
    s = novelai_settings
    prompt = req.prompt
    notes = []
    if s.translate and has_chinese(prompt):
        prompt = await to_novelai_tags(prompt)
        notes.append(f"tag：{prompt}")
    artists = str(s.default_artists) if inputs.artists is None else inputs.artists
    reference = None
    if inputs.references:
        if reason := check_reference_supported("novelai"):
            raise ServiceError(reason)
        canvas, _ = images.letterbox(inputs.references[0], novelai.REFERENCE_CANVASES)
        reference = novelai.CharacterReference(
            image=canvas,
            with_style=req.with_style,
            strength=float(s.reference_strength),
            fidelity=float(s.reference_fidelity),
        )
        notes.append("参考：角色＋画风" if req.with_style else "参考：角色")
    width, height = novelai.SIZES[req.size]
    image, seed = await novelai.generate(
        novelai.NovelAIRequest(
            prompt=compose(str(s.positive_prefix), artists, prompt),
            negative=str(s.negative),
            model=str(s.model),
            width=width,
            height=height,
            steps=int(s.steps),
            scale=float(s.scale),
            sampler=str(s.sampler),
            reference=reference,
        ),
        token=str(s.token),
        base_url=str(s.base_url),
        proxy=str(draw_settings.proxy),
        timeout=float(s.timeout),
    )
    logger.info("[AI画图] NovelAI 完成：model={}, size={}, seed={}, 参考图={}, 自定义画师={}",
                s.model, req.size, seed, reference is not None, inputs.artists is not None)
    return DrawResult(image=image, provider="NovelAI", prompt_used=prompt, note="\n".join(notes))


async def draw_gpt(req: DrawRequest, inputs: DrawInputs) -> DrawResult:
    s = gpt_image_settings
    refs = [images.normalize(img)[0] for img in inputs.references[: gpt_image.MAX_REFERENCES]]
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
        references=refs,
    )
    logger.info("[AI画图] GPT Image 完成：model={}, size={}, 参考图={}", s.model, req.size, len(refs))
    return DrawResult(image=image, provider="GPT Image", prompt_used=req.prompt,
                      note=f"参考了 {len(refs)} 张图" if refs else "")


async def draw(provider: str, req: DrawRequest, inputs: DrawInputs | None = None) -> DrawResult:
    inputs = inputs or DrawInputs()
    if provider == "novelai":
        return await draw_novelai(req, inputs)
    if provider == "gpt":
        return await draw_gpt(req, inputs)
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
        token=str(s.token), base_url=str(s.base_url), proxy=str(draw_settings.proxy)
    )


async def test_gpt_image() -> str:
    s = gpt_image_settings
    return await gpt_image.check_models(
        api_key=str(s.api_key), base_url=str(s.base_url), model=str(s.model), proxy=str(draw_settings.proxy)
    )
