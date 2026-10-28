"""工具定义。每个工具：校验参数 → 后台执行 → 把结果发到会话里。

确认的话由凛音自己说（MaiBot 的回复），这里不再发词库里的确认词。

接口马上返回给凛音一句说明（「已经开始画了」），不让 MaiBot 等几十秒。
耗时任务失败时，由 NoneBot 在会话里说明原因。
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .._shared import qq_images
from .._shared.services.http import describe_exception

_ID = re.compile(r"^\d{5,12}$")
MAX_TEXT = 5000

# 后台任务的引用，防止被垃圾回收。
_background: set[asyncio.Task] = set()


class ToolError(Exception):
    """参数不对或功能不可用。message 会原样返回给凛音。"""


@dataclass
class ToolContext:
    user_id: str
    group_id: str | None
    user_name: str = ""
    image_urls: list[str] = field(default_factory=list)
    reply_to: str | None = None  # 被回复的消息 ID，结果会引用这条消息

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> ToolContext:
        user_id = str(body.get("user_id") or "").strip()
        group_id = str(body.get("group_id") or "").strip() or None
        if not _ID.match(user_id):
            raise ToolError("缺少发起人的 QQ 号")
        if group_id is not None and not _ID.match(group_id):
            raise ToolError("群号格式不正确")
        urls = [str(u) for u in (body.get("image_urls") or []) if isinstance(u, str)][:4]
        reply_to = str(body.get("message_id") or "").strip()
        return cls(
            user_id=user_id,
            group_id=group_id,
            user_name=str(body.get("user_name") or "")[:40],
            image_urls=[u for u in urls if qq_images.host_allowed(u)],
            reply_to=reply_to if reply_to.isdigit() else None,
        )


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, dict[str, Any]]
    handler: Handler

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "params": self.params}

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - set(self.params)
        if unknown:
            raise ToolError(f"未知参数：{'、'.join(sorted(unknown))}")
        missing = [k for k, p in self.params.items() if p.get("required") and args.get(k) in (None, "")]
        if missing:
            raise ToolError(f"缺少参数：{'、'.join(missing)}")
        return await self.handler(ctx, args)


# ---------------------------------------------------------------- 发送


async def _bot():
    from nonebot import get_bots
    from nonebot.adapters.onebot.v11 import Bot

    for bot in get_bots().values():
        if isinstance(bot, Bot):
            return bot
    raise ToolError("QQ 机器人当前不在线")


async def send(ctx: ToolContext, message: str | Message, *, quote: bool = False) -> None:
    bot = await _bot()
    msg = Message(message) if isinstance(message, str) else message
    if quote and ctx.reply_to:
        msg = MessageSegment.reply(int(ctx.reply_to)) + msg
    if ctx.group_id:
        await bot.send_group_msg(group_id=int(ctx.group_id), message=msg)
    else:
        await bot.send_private_msg(user_id=int(ctx.user_id), message=msg)


def _spawn(name: str, coro: Awaitable[None]) -> None:
    async def runner():
        try:
            await coro
        except Exception as e:  # 后台任务的异常不能丢
            logger.opt(exception=e).warning("[工具接口] {} 后台任务失败", name)

    task = asyncio.get_running_loop().create_task(runner())
    _background.add(task)
    task.add_done_callback(_background.discard)


def _text(args: dict[str, Any], key: str, *, limit: int = MAX_TEXT) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolError(f"{key} 必须是文字")
    value = value.strip()
    if len(value) > limit:
        raise ToolError(f"{key} 太长了，最多 {limit} 个字")
    return value


def _choice(args: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = args.get(key) or default
    if value not in allowed:
        raise ToolError(f"{key} 只能是 {'、'.join(allowed)}")
    return value


def _int(args: dict[str, Any], key: str, *, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ToolError(f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{key} 必须是整数") from None
    return max(low, min(high, number))


async def _download_references(ctx: ToolContext) -> list[bytes]:
    return [await qq_images.download(url) for url in ctx.image_urls[:1]]


# ---------------------------------------------------------------- 画图


SIZES = ("竖图", "横图", "方图")


async def tool_draw(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..ai_draw_plugin.config import draw_settings
    from ..ai_draw_plugin.prompting import DrawRequest
    from ..ai_draw_plugin.service import DrawInputs, check_reference_supported, draw
    from ..ai_draw_plugin.storage import artist_store

    if not draw_settings.enabled:
        raise ToolError("画图功能已关闭")
    prompt = _text(args, "prompt")
    if not prompt:
        raise ToolError("缺少画面描述")
    provider = _choice(args, "backend", ("novelai", "gpt", "default"), "default")
    if provider == "default":
        provider = str(draw_settings.default_provider)
    size = _choice(args, "size", SIZES, "竖图")
    use_reference = bool(args.get("use_reference")) and bool(ctx.image_urls)
    if use_reference and (reason := check_reference_supported(provider)):
        raise ToolError(reason)

    async def job():
        inputs = DrawInputs()
        if provider == "novelai" and (saved := artist_store.get(ctx.user_id)):
            inputs.artists = saved
        try:
            if use_reference:
                inputs.references = await _download_references(ctx)
            result = await draw(provider, DrawRequest(size=size, prompt=prompt), inputs)
        except Exception as e:
            logger.opt(exception=e).warning("[凛音工具] draw 失败：provider={}", provider)
            name = "NovelAI" if provider == "novelai" else "GPT Image"
            await send(ctx, f"……抱歉，这次没能画好。{describe_exception(name, e)}")
            return
        await send(ctx, Message(MessageSegment.image(result.image)), quote=True)

    _spawn("draw", job())
    return {"content": "已经开始画了，图片稍后会直接发到聊天里。用你自己的口吻回应对方，不用再描述画面。"}


# ---------------------------------------------------------------- 自拍


async def tool_selfie(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from .selfie import selfie_ready, take_selfie

    scene = _text(args, "scene")
    if not scene:
        raise ToolError("缺少自拍的情景描述")
    if reason := selfie_ready():
        raise ToolError(reason)

    async def job():
        try:
            image = await take_selfie(scene)
        except Exception as e:
            logger.opt(exception=e).warning("[凛音工具] selfie 失败")
            await send(ctx, f"……今天状态不太好，下次再拍吧。（{describe_exception('自拍', e)}）")
            return
        await send(ctx, Message(MessageSegment.image(image)), quote=True)

    _spawn("selfie", job())
    return {"content": "自拍已经在准备了，照片稍后会直接发到聊天里。用你自己的口吻说一句符合当下心情的话。"}


# ---------------------------------------------------------------- 语音


def _is_superuser(user_id: str) -> bool:
    from nonebot import get_driver

    superusers = get_driver().config.superusers
    return user_id in superusers or f"onebot:{user_id}" in superusers


async def tool_tts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..tts_plugin.service import char_limit, is_ready, is_singing, spoken_length, synthesize

    superuser = _is_superuser(ctx.user_id)
    limit = char_limit(superuser=superuser)
    # [情感提示] 不计字数，原文留出余量，实际字数在下面按要读的字检查
    text = _text(args, "text", limit=limit * 2 + 200)
    if not text:
        raise ToolError("缺少要说的话")
    if args.get("sing") is True and not is_singing(text):
        text = f"[singing] {text}"
    if is_singing(text) and not superuser:
        raise ToolError("唱歌只有主人（管理员）能点，其他人请用自己的话婉拒")
    if spoken_length(text) > limit:
        if superuser:
            raise ToolError(f"太长了，最多 {limit} 个字，请缩短或分成几次说")
        raise ToolError(f"太长了，最多 {limit} 个字；大段语音只有主人（管理员）能点，请缩短到一两句")
    if not is_ready():
        raise ToolError("语音功能没有开启或没有配置")

    async def job():
        try:
            audio = await synthesize(text, superuser=superuser)
        except Exception as e:
            await send(ctx, f"嗓子不太舒服……{describe_exception('Fish Audio', e)}")
            return
        await send(ctx, Message(MessageSegment.record(audio)))

    _spawn("tts", job())
    return {
        "content": "语音正在合成，稍后会直接发出。这句话已经用语音说了，不要再用文字重复一遍；可以不回复，或只回一个很短的动作。",
        "stop_after_execution": True,
    }


# ---------------------------------------------------------------- 投骰


async def tool_dice(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..dice_girl_plugin.dice import dice_logic

    faces = _int(args, "faces", default=100, low=2, high=10000)
    target = args.get("target")
    target = None if target in (None, "") else _int(args, "target", default=50, low=1, high=faces)
    event_name = _text(args, "event", limit=30) or "随机数"
    result = dice_logic.execute_roll(faces, target, event_name)

    header = f"1D{faces}={result['final_roll']}"
    if target is not None:
        header += f"/{target} 【{result['final_status']}】"
    await send(ctx, f"{event_name}：{header}", quote=True)
    return {
        "content": (
            f"已经把投骰结果发到聊天里了：{event_name} 1D{faces} 掷出 {result['final_roll']}"
            + (f"，目标 {target}，结果「{result['final_status']}」" if target is not None else "")
            + "。结果是真随机的，不能更改。请用自己的口吻简短点评，不要重复报数字。"
        ),
        "roll": result["final_roll"],
        "status": result["final_status"],
    }


# ---------------------------------------------------------------- Pixiv


async def _pixiv_target(ctx: ToolContext):
    from nonebot_plugin_alconna.uniseg import Target

    return Target.group(ctx.group_id) if ctx.group_id else Target.user(ctx.user_id)


async def tool_pixiv_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..pixiv_bot_plugin.parser.search import build_search_params
    from ..pixiv_bot_plugin.pixiv import get_pixiv_spider
    from ..pixiv_bot_plugin.renderers import send_images_with_info
    from ..pixiv_bot_plugin.services.core import search_images

    tags = _text(args, "tags", limit=60)
    count = _int(args, "count", default=1, low=1, high=3)
    mode = _choice(args, "mode", ("random", "recent", "popular"), "random")
    # 凛音常把「500users入り」写进标签，要拆成收藏筛选；原样搜会和随机模式自带的
    # 「100users入り」叠在一起，Pixiv 要求两个标签同时存在，结果就是空的
    params = build_search_params(tags, count, mode)
    if params is None:
        raise ToolError("缺少搜索标签")
    tags = params.display_tags
    target = await _pixiv_target(ctx)

    async def job():
        try:
            async with get_pixiv_spider() as spider:
                result = await search_images(spider, params)
            if not result.ok:
                await send(ctx, result.message)
                return
            await send_images_with_info(result.images or [], result.title, target=target)
        except Exception as e:
            await send(ctx, f"书架上没找到……{describe_exception('Pixiv', e)}")

    _spawn("pixiv_search", job())
    return {"content": f"正在 Pixiv 上找「{tags}」，图片稍后会直接发到聊天里。用你自己的口吻简短回应。"}


async def tool_pixiv_daily(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..pixiv_bot_plugin.pixiv import get_pixiv_spider
    from ..pixiv_bot_plugin.renderers import send_images_with_info
    from ..pixiv_bot_plugin.services.core import daily_recommend

    target = await _pixiv_target(ctx)

    async def job():
        try:
            async with get_pixiv_spider() as spider:
                result = await daily_recommend(spider, ctx.user_id)
            if not result.ok:
                await send(ctx, result.message)
                return
            await send_images_with_info(result.images or [], result.title, target=target)
        except Exception as e:
            await send(ctx, f"今天的推荐没能取来……{describe_exception('Pixiv', e)}")

    _spawn("pixiv_daily", job())
    return {"content": "今日推荐稍后会直接发到聊天里。用你自己的口吻简短回应。"}


# ---------------------------------------------------------------- 说说


async def tool_qzone(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """写一条说说并配自拍，把正文和图片返回给 MaiBot，由它通过 Maizone 发到 QQ 空间。

    check_only 时只检查能不能发，凛音马上就能知道要不要婉拒；真正生成要一两分钟。
    """
    from .qzone import qzone_ready, remember, write_post
    from .selfie import selfie_ready, take_selfie

    if not _is_superuser(ctx.user_id):
        raise ToolError("发说说只有管理员（superuser）能让你发，这个人不是，请用自己的话婉拒")
    if reason := qzone_ready() or selfie_ready():
        raise ToolError(reason)
    topic = _text(args, "topic", limit=100)
    mood = _text(args, "mood", limit=100)[:20]
    slot = _text(args, "slot", limit=20)
    if args.get("check_only") is True:
        return {"content": "可以发"}

    try:
        post = await write_post(topic=topic, mood=mood, slot=slot)
        image = await take_selfie(post.selfie_scene)
    except Exception as e:
        logger.opt(exception=e).warning("[凛音工具] qzone 失败")
        raise ToolError(describe_exception("说说", e)) from None
    remember(post.text)
    return {
        "content": "说说已经写好",
        "text": post.text,
        "mood": post.mood,
        "image": base64.b64encode(image).decode("ascii"),
    }


# ---------------------------------------------------------------- 聊天预设


async def tool_preset(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """MaiBot 每次回复前调用：这个会话要额外套用的预设提示词。没有生效的模式时 prompt 为空。"""
    from .._shared.presets import prompt_for

    prompt, modes = prompt_for(ctx.user_id, ctx.group_id, ctx.user_name)
    return {"content": "", "prompt": prompt, "modes": modes}


# ---------------------------------------------------------------- 注册表


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "draw",
            "画一张插画",
            {
                "prompt": {"type": "string", "required": True},
                "backend": {"type": "string", "enum": ["default", "novelai", "gpt"]},
                "size": {"type": "string", "enum": list(SIZES)},
                "use_reference": {"type": "boolean"},
            },
            tool_draw,
        ),
        ToolSpec("selfie", "凛音的自拍", {"scene": {"type": "string", "required": True}}, tool_selfie),
        ToolSpec(
            "tts",
            "用语音说话或唱歌",
            {"text": {"type": "string", "required": True}, "sing": {"type": "boolean"}},
            tool_tts,
        ),
        ToolSpec(
            "dice",
            "投骰子",
            {
                "faces": {"type": "integer"},
                "target": {"type": "integer"},
                "event": {"type": "string"},
            },
            tool_dice,
        ),
        ToolSpec(
            "pixiv_search",
            "在 Pixiv 搜图",
            {
                "tags": {"type": "string", "required": True},
                "count": {"type": "integer"},
                "mode": {"type": "string", "enum": ["random", "recent", "popular"]},
            },
            tool_pixiv_search,
        ),
        ToolSpec("pixiv_daily", "Pixiv 每日推荐", {}, tool_pixiv_daily),
        ToolSpec(
            "qzone",
            "写一条说说并配自拍（只返回内容，不发送）",
            {
                "topic": {"type": "string"},
                "mood": {"type": "string"},
                "slot": {"type": "string", "enum": ["work", "off_work", "bedtime"]},
                "check_only": {"type": "boolean"},
            },
            tool_qzone,
        ),
        ToolSpec("preset", "取当前会话的聊天预设提示词（回复前调用，不是给凛音用的工具）", {}, tool_preset),
    )
}
