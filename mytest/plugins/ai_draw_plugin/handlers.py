from __future__ import annotations

from nonebot import logger, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot_plugin_waiter import prompt

from .._shared import qq_images
from .._shared.events import group_id_of, is_superuser
from .._shared.limits import UsageLimiter, scope_allowed
from .._shared.services.http import ServiceError, describe_exception
from .artists import MAX_LENGTH, clean, is_clear_word
from .config import draw_settings, novelai_settings
from .prompting import parse_request
from .service import DrawInputs, check_reference_supported, draw
from .storage import artist_store

limiter = UsageLimiter()

WAIT_SECONDS = 120
CANCEL_WORDS = {"取消", "算了", "不画了", "cancel"}
PROVIDER_NAMES = {"novelai": "NovelAI", "gpt": "GPT Image"}

USAGE = (
    "用法：/画图 描述\n"
    "指定后端：/nai 描述、/gpt画图 描述\n"
    "尺寸：描述前加「横图」或「方图」，默认竖图\n"
    "参考图：直接带图发送，或回复一张图；加 -参考 会提示你发图\n"
    "画师串（NovelAI）：加 -画师 会询问新的画师串\n"
    "例：/nai 横图 海边的白发少女，夕阳"
)

draw_matcher = on_regex(r"^/(画图|nai|gpt画图)(?:\s+([\s\S]*))?$", priority=5, block=True)
artist_matcher = on_regex(r"^/nai画师(?:\s+([\s\S]*))?$", priority=4, block=True)


def _text_of(message: Message | None) -> str:
    return message.extract_plain_text().strip() if message is not None else ""


async def _ask_reference() -> bytes | None:
    """引导用户发一张图。返回图片字节；取消或超时返回 None。"""
    reply = await prompt(f"请发送一张参考图（{WAIT_SECONDS} 秒内，发「取消」放弃）", timeout=WAIT_SECONDS)
    if reply is None or _text_of(reply) in CANCEL_WORDS:
        return None
    urls = qq_images.image_urls(reply)
    if not urls:
        raise ServiceError("没有收到图片，已取消")
    return await qq_images.download(urls[0])


async def _ask_artists(current: str) -> str | None:
    """引导用户输入新的画师串。返回画师串；取消或超时返回 None。"""
    shown = current or "（没有）"
    reply = await prompt(
        f"当前画师串：{shown}\n请发送新的画师串（{WAIT_SECONDS} 秒内，发「取消」放弃）。\n"
        "例：artist:wlop, {artist:ask (askzy)}, 0.8::artist:mika pikazo::",
        timeout=WAIT_SECONDS,
    )
    text = _text_of(reply)
    if reply is None or text in CANCEL_WORDS:
        return None
    artists = clean(text)
    if not artists:
        raise ServiceError("画师串是空的，已取消")
    if len(artists) > MAX_LENGTH:
        raise ServiceError(f"画师串太长了，最多 {MAX_LENGTH} 个字符")
    return artists


@draw_matcher.handle()
async def handle_draw(bot: Bot, event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    if not draw_settings.enabled:
        await matcher.finish()

    command, text = groups[0], (groups[1] or "").strip()
    group_id = group_id_of(event)
    if not scope_allowed(
        group_id, allowed_groups=list(draw_settings.allowed_groups), allow_private=draw_settings.allow_private
    ):
        await matcher.finish()

    req = parse_request(text)
    if not req.prompt:
        await matcher.finish(USAGE)

    provider = {"nai": "novelai", "gpt画图": "gpt"}.get(command, str(draw_settings.default_provider))
    name = PROVIDER_NAMES.get(provider, provider)
    user_id = event.get_user_id()
    attached = qq_images.event_image_urls(event)

    # 先检查能不能用，免得问完参考图才发现模型不支持。
    if (attached or req.wants_reference) and (reason := check_reference_supported(provider)):
        await matcher.finish(reason)
    if req.wants_artist and provider != "novelai":
        await matcher.finish("-画师 只对 NovelAI 有效，GPT Image 请直接在描述里写画风")

    inputs = DrawInputs()
    try:
        # 引导只在主动加了参数时发生；平时直接出图。
        if req.wants_artist:
            artists = await _ask_artists(artist_store.get(user_id) or str(novelai_settings.default_artists))
            if artists is None:
                await matcher.finish("已取消")
            inputs.artists = artist_store.set(user_id, artists)
            await matcher.send("已记住你的画师串，以后画图都会用它（/nai画师 默认 可恢复默认）")
        elif provider == "novelai" and (saved := artist_store.get(user_id)):
            inputs.artists = saved

        if attached:
            inputs.references.append(await qq_images.download(attached[0]))
        elif req.wants_reference:
            image = await _ask_reference()
            if image is None:
                await matcher.finish("已取消")
            inputs.references.append(image)
    except ServiceError as e:
        await matcher.finish(str(e))

    reason = await limiter.try_acquire(
        user_id,
        cooldown=float(draw_settings.cooldown),
        max_concurrent=int(draw_settings.max_concurrent),
        exempt=await is_superuser(bot, event),
    )
    if reason:
        await matcher.finish(reason)

    success = False
    try:
        await matcher.send("收到，正在画……")
        result = await draw(provider, req, inputs)
        message = Message(MessageSegment.reply(event.message_id)) if hasattr(event, "message_id") else Message()
        message += MessageSegment.image(result.image)
        if result.note:
            message += MessageSegment.text(f"\n{result.note[:300]}")
        success = True
    except Exception as e:
        logger.opt(exception=e).warning("[AI画图] 失败：provider={}", provider)
        await matcher.finish(f"画图失败：{describe_exception(name, e)}")
    finally:
        limiter.release(user_id, success=success)
    await matcher.finish(message)


@artist_matcher.handle()
async def handle_artist(event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    if not draw_settings.enabled:
        await matcher.finish()
    user_id = event.get_user_id()
    text = (groups[0] or "").strip()
    default = str(novelai_settings.default_artists).strip()

    if not text:
        mine = artist_store.get(user_id)
        lines = [f"你的画师串：{mine}" if mine else "你没有设置自己的画师串，正在用默认的。"]
        lines.append(f"默认画师串：{default or '（没有）'}")
        lines.append("修改：/nai画师 新画师串　恢复默认：/nai画师 默认")
        await matcher.finish("\n".join(lines))

    if is_clear_word(text):
        cleared = artist_store.clear(user_id)
        await matcher.finish("已恢复为默认画师串" if cleared else "你本来就在用默认画师串")

    try:
        artists = artist_store.set(user_id, text)
    except ValueError as e:
        await matcher.finish(str(e))
    await matcher.finish(f"已记住你的画师串：{artists}")
