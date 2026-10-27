from __future__ import annotations

from nonebot import logger, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup

from .._shared.events import group_id_of, is_superuser
from .._shared.limits import UsageLimiter, scope_allowed
from .._shared.services.http import describe_exception
from .config import draw_settings
from .prompting import parse_request
from .service import draw

limiter = UsageLimiter()

USAGE = (
    "用法：/画图 描述\n"
    "指定后端：/nai 描述、/gpt画图 描述\n"
    "尺寸：描述前加「横图」或「方图」，默认竖图\n"
    "例：/nai 横图 海边的白发少女，夕阳"
)

draw_matcher = on_regex(
    r"^/(画图|nai|gpt画图)(?:\s+([\s\S]*))?$",
    priority=5,
    block=True,
)


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
    user_id = event.get_user_id()
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
        result = await draw(provider, req)
        message = Message(MessageSegment.reply(event.message_id)) if hasattr(event, "message_id") else Message()
        message += MessageSegment.image(result.image)
        if result.note:
            message += MessageSegment.text(f"\n{result.note[:300]}")
        success = True
    except Exception as e:
        logger.opt(exception=e).warning("[AI画图] 失败：provider={}", provider)
        name = "NovelAI" if provider == "novelai" else "GPT Image"
        await matcher.finish(f"画图失败：{describe_exception(name, e)}")
    finally:
        limiter.release(user_id, success=success)
    await matcher.finish(message)
