from __future__ import annotations

from nonebot import logger, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup

from .._shared.events import group_id_of, is_superuser
from .._shared.limits import UsageLimiter, scope_allowed
from .._shared.phrases import pick
from .._shared.services.http import describe_exception
from .config import tts_settings
from .service import is_singing, synthesize

limiter = UsageLimiter()

say_matcher = on_regex(r"^/说(?:\s+([\s\S]*))?$", priority=5, block=True)


@say_matcher.handle()
async def handle_say(bot: Bot, event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    if not tts_settings.enabled:
        await matcher.finish()
    if not scope_allowed(
        group_id_of(event),
        allowed_groups=list(tts_settings.allowed_groups),
        allow_private=tts_settings.allow_private,
    ):
        await matcher.finish()

    text = (groups[0] or "").strip()
    if not text:
        await matcher.finish("用法：/说 要朗读的文字")

    user_id = event.get_user_id()
    superuser = await is_superuser(bot, event)
    if not superuser and is_singing(text):
        await matcher.finish("唱歌只有管理员可以用。")
    reason = await limiter.try_acquire(
        user_id,
        cooldown=float(tts_settings.cooldown),
        max_concurrent=3,
        exempt=superuser,
    )
    if reason:
        await matcher.finish(reason)

    success = False
    try:
        await matcher.send(pick("tts"))
        audio = await synthesize(text, superuser=superuser)
        success = True
    except Exception as e:
        logger.opt(exception=e).warning("[语音] 合成失败")
        await matcher.finish(f"语音合成失败：{describe_exception('Fish Audio', e)}")
    finally:
        limiter.release(user_id, success=success)
    await matcher.finish(MessageSegment.record(audio))
