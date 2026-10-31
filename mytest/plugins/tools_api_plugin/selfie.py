"""凛音的自拍：用 WebUI 上传的人设图做参考，按情景生成新图。"""

from __future__ import annotations

from pathlib import Path

from nonebot import logger

import io

from .._shared.services import gpt_image, images, novelai
from .._shared.services.http import ServiceError
from .._shared.settings import FileSlot, SettingField, SettingSection, proxy_for, register
from .._shared.settings import settings_store as _store

selfie_settings = register(
    SettingSection(
        id="selfie",
        title="凛音自拍",
        description="群友让凛音发自拍时，凛音根据聊天内容想一个情景，再参照人设图画出来。人设图在下面上传。",
        order=35,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField("backend", "用哪个画", type="select", default="novelai",
                         options=(("novelai", "NovelAI（二次元画风稳定，需要 V4.5 模型）"), ("gpt", "GPT Image"))),
            SettingField(
                "base_prompt",
                "人设描述",
                type="textarea",
                default=(
                    "1girl, solo, shirotori rinne, light blonde long hair, blue-grey eyes"
                ),
                help="每次自拍都会加上的固定描述，只写外貌；服装、构图、看不看镜头由情景决定，写在这里会让每张图都一样。"
                "NovelAI 用英文 tag；GPT Image 也能读懂",
            ),
            SettingField("novelai_model", "NovelAI 模型", type="select", default=novelai.REFERENCE_MODELS[0],
                         options=tuple(m for m in novelai.MODELS if novelai.supports_reference(m[0])),
                         help="自拍要用人设图做角色参考，只有 V4.5 支持；和画图用的模型分开设置，"
                         "其余参数（步数等）沿用 NovelAI 的设置"),
            SettingField("artists", "画师串", type="textarea", default="",
                         help="仅 NovelAI：只用在自拍上；留空沿用 NovelAI 的默认画师串"),
            SettingField("with_style", "连画风一起参考", type="bool", default=True,
                         help="仅 NovelAI：打开后画风也贴近人设图"),
        ],
    )
)


def persona_path() -> Path:
    return _store.path.parent / "rinne_persona.png"


def save_persona(data: bytes) -> tuple[int, int]:
    """校验并保存人设图，返回图片尺寸。"""
    png, _ = images.normalize(data, max_side=2048)
    from PIL import Image

    size = Image.open(io.BytesIO(png)).size
    path = persona_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(path)
    logger.info("[自拍] 已更新人设图 {}x{}", *size)
    return size


def save_persona_from_webui(data: bytes) -> str:
    try:
        width, height = save_persona(data)
    except ServiceError as e:
        raise ValueError(str(e)) from None
    return f"人设图已更新（{width}×{height}）"


_store.add_file_slot(
    "selfie",
    FileSlot(
        key="persona",
        label="人设图",
        path=persona_path,
        save=save_persona_from_webui,
        help="建议用全身、站姿自然、背景简单的图；脸部清晰最好。PNG / JPG / WebP，最大 20 MB",
    ),
)


def selfie_ready() -> str:
    """能自拍返回空字符串，否则返回原因。"""
    if not selfie_settings.enabled:
        return "自拍功能已关闭"
    if not persona_path().exists():
        return "还没有上传凛音的人设图，请管理员在 WebUI 的「凛音自拍」里上传"
    return ""


def selfie_model() -> str:
    """自拍固定用支持角色参考的模型，不跟随画图的模型设置。"""
    model = str(selfie_settings.novelai_model)
    return model if novelai.supports_reference(model) else novelai.REFERENCE_MODELS[0]


async def take_selfie(scene: str) -> bytes:
    from ..ai_draw_plugin.artists import compose
    from ..ai_draw_plugin.config import draw_settings, gpt_image_settings, novelai_settings
    from ..ai_draw_plugin.prompting import has_chinese, to_novelai_tags

    if reason := selfie_ready():
        raise ServiceError(reason)
    persona = persona_path().read_bytes()
    base = str(selfie_settings.base_prompt).strip()

    if str(selfie_settings.backend) == "gpt":
        s = gpt_image_settings
        prompt = (
            "这是同一个角色的照片。保持参考图里角色的长相、发色和画风一致；"
            "服装、发型、姿势、表情、机位和取景都按这次的情景来，不要照搬参考图的站姿和构图。"
            f"\n角色设定：{base}\n这次的情景：{scene}"
        )
        return await gpt_image.generate(
            prompt,
            api_key=str(s.api_key),
            base_url=str(s.base_url),
            model=str(s.model),
            size=gpt_image.SIZES["竖图"],
            quality=str(s.quality),
            moderation=str(s.moderation),
            proxy=proxy_for(draw_settings),
            timeout=float(s.timeout),
            references=[images.normalize(persona)[0]],
        )

    s = novelai_settings
    scene_tags = await to_novelai_tags(scene) if s.translate and has_chinese(scene) else scene
    artists = str(selfie_settings.artists).strip() or str(s.default_artists)
    canvas, _ = images.letterbox(persona, novelai.REFERENCE_CANVASES)
    width, height = novelai.SIZES["竖图"]
    image, _seed = await novelai.generate(
        novelai.NovelAIRequest(
            prompt=compose(str(s.positive_prefix), artists, f"{base}, {scene_tags}"),
            negative=str(s.negative),
            model=selfie_model(),
            width=width,
            height=height,
            steps=int(s.steps),
            scale=float(s.scale),
            sampler=str(s.sampler),
            reference=novelai.CharacterReference(
                image=canvas,
                with_style=bool(selfie_settings.with_style),
                strength=float(s.reference_strength),
                fidelity=float(s.reference_fidelity),
            ),
        ),
        token=str(s.token),
        base_url=str(s.base_url),
        proxy=proxy_for(draw_settings),
        timeout=float(s.timeout),
    )
    return image


async def list_persona_status() -> str:
    """「测试连接」按钮：检查人设图和后端是否就绪。"""
    if reason := selfie_ready():
        raise ServiceError(reason)
    from PIL import Image

    size = Image.open(io.BytesIO(persona_path().read_bytes())).size
    backend = "NovelAI" if str(selfie_settings.backend) == "novelai" else "GPT Image"
    return f"已就绪：人设图 {size[0]}×{size[1]}，使用 {backend}"
