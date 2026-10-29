"""工具定义。每个工具：校验参数 → 后台执行 → 把结果发到会话里。

确认的话由凛音自己说（MaiBot 的回复），这里不再发词库里的确认词。

接口马上返回给凛音一句说明（「已经开始画了」），不让 MaiBot 等几十秒。
耗时任务失败时，由 NoneBot 在会话里说明原因。
"""

from __future__ import annotations

import asyncio
import base64
import random
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
    from ..tts_plugin.service import (
        char_limit,
        is_ready,
        is_singing,
        spoken_length,
        synthesize,
    )

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


def _photo_ready() -> str:
    """能画「随手拍的东西」返回空字符串，否则返回原因。用「AI 画图」的默认后端。"""
    from ..ai_draw_plugin.config import draw_settings

    return "" if draw_settings.enabled else "画图功能已关闭"


async def _take_photo(prompt: str) -> bytes:
    from ..ai_draw_plugin.config import draw_settings
    from ..ai_draw_plugin.prompting import DrawRequest
    from ..ai_draw_plugin.service import draw

    result = await draw(str(draw_settings.default_provider), DrawRequest(size="方图", prompt=prompt))
    return result.image


async def tool_qzone(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """写一条说说并配图（自拍或随手拍的东西），把正文和图片返回给 MaiBot，由它通过 Maizone 发到 QQ 空间。

    check_only 时只检查能不能发，凛音马上就能知道要不要婉拒；真正生成要一两分钟。
    """
    from .qzone import PHOTO, pick_photo, qzone_ready, remember, write_post
    from .selfie import selfie_ready, take_selfie

    if not _is_superuser(ctx.user_id):
        raise ToolError("发说说只有管理员（superuser）能让你发，这个人不是，请用自己的话婉拒")
    if reason := qzone_ready():
        raise ToolError(reason)
    selfie_reason, photo_reason = selfie_ready(), _photo_ready()
    if selfie_reason and photo_reason:
        raise ToolError(f"配不了图：{selfie_reason}，{photo_reason}")
    topic = _text(args, "topic", limit=100)
    mood = _text(args, "mood", limit=100)[:20]
    slot = _text(args, "slot", limit=20)
    if args.get("check_only") is True:
        return {"content": "可以发"}

    photo = pick_photo(random.Random(), can_selfie=not selfie_reason, can_photo=not photo_reason)
    try:
        post = await write_post(topic=topic, mood=mood, slot=slot, photo=photo)
        image = await (_take_photo(post.photo_prompt) if photo == PHOTO else take_selfie(post.selfie_scene))
    except Exception as e:
        logger.opt(exception=e).warning("[凛音工具] qzone 失败")
        raise ToolError(describe_exception("说说", e)) from None
    remember(post.text)
    return {
        "content": "说说已经写好",
        "text": post.text,
        "mood": post.mood,
        "photo": photo,
        "image": base64.b64encode(image).decode("ascii"),
    }


# ---------------------------------------------------------------- 一起听


def _listen():
    """一起听的服务；没启用时返回 None。插件在同一个目录里，启动时已经加载。"""
    try:
        from ..listen_plugin import service
    except ImportError:
        return None
    return service


def _listen_or_fail():
    service = _listen()
    if service is None:
        raise ToolError("一起听没有开启")
    return service


def _fmt_ms(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def listen_context(group_id: str | None) -> str:
    """给凛音回复前注入的一小段话：本群正在一起听什么。没有在听返回空字符串。"""
    service = _listen()
    if service is None or not group_id or not service.group_allowed(group_id):
        return ""
    now = service.now_playing(group_id)
    if not now:
        return ""
    text = f"本群正在一起听：{now['current']}（{_fmt_ms(now['position_ms'])}/{_fmt_ms(now['duration_ms'])}"
    text += "，暂停中" if now["paused"] else ""
    text += f"，{now['added_by']}点的）" if now["added_by"] else "）"
    if now["next"]:
        text += "，接下来：" + "、".join(now["next"])
    return text + f"。{now['listeners']} 人在听。"


async def tool_listen_context(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"content": listen_context(ctx.group_id)}


async def tool_listen_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = _listen_or_fail()
    if not ctx.group_id or not service.group_allowed(ctx.group_id):
        raise ToolError("这个群没有开一起听")
    status = service.listen_status(ctx.group_id)
    if not status["open"]:
        return {"content": "本群现在没有一起听。想听的话在群里发 /一起听 开一个。"}
    lines = []
    now = status["now"]
    if now:
        lines.append(f"正在放：{now['current']}（{_fmt_ms(now['position_ms'])}/{_fmt_ms(now['duration_ms'])}"
                     + ("，暂停中）" if now["paused"] else "）"))
    else:
        lines.append("现在没在放歌。")
    if status["queue"]:
        lines.append("接下来：" + "；".join(f"{q['song']}（{q['by']}点的）" for q in status["queue"]))
    if status["history"]:
        lines.append("刚放过：" + "；".join(status["history"]))
    if status["members"]:
        lines.append("在听的人：" + "、".join(status["members"]))
    return {"content": "\n".join(lines) + "\n按对方问的简短回答，不要把整份列表念出来。"}


async def _send_card(ctx: ToolContext, track: Any) -> None:
    from ..listen_plugin.card import build_card

    service = _listen_or_fail()
    card = build_card(track, await service.card_audio(track))
    try:
        # NapCat：不带 id 的 music 段按自定义卡片签名，type 填平台决定角标（见 listen_plugin/card.py）
        await send(ctx, Message(MessageSegment("music", card.segment_data())))
    except Exception as e:  # noqa: BLE001 签名服务挂了、NapCat 不认：退回普通消息
        logger.info("[凛音工具] 音乐卡片发送失败，改发文字：{}", e)
        msg = Message(MessageSegment.text(card.fallback_text()))
        if card.image:
            msg = Message(MessageSegment.image(card.image)) + msg
        await send(ctx, msg)


async def tool_music_share(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """有人让凛音发某首歌：搜一首，发音乐卡片。所有群都能用。"""
    service = _listen_or_fail()
    keyword = _text(args, "song", limit=80)
    if not keyword:
        raise ToolError("缺少歌名")
    track = await service.resolve_song(keyword)
    if track is None:
        raise ToolError(f"三个平台都没搜到「{keyword}」")

    async def job():
        try:
            await _send_card(ctx, track)
        except Exception as e:
            logger.opt(exception=e).warning("[凛音工具] music_share 失败")
            await send(ctx, f"歌没发出去……{describe_exception('音乐', e)}")

    _spawn("music_share", job())
    return {"content": f"找到了「{track.label()}」，卡片马上发出去。用你自己的口吻简短说一句，不要再报歌名。"}


async def tool_music_recommend(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """有人让凛音推荐歌：按要求选歌、核对存在。白名单群开着一起听时加进队列，否则发卡片。"""
    from ..listen_plugin.recommend import RecommendRequest, recommend

    service = _listen_or_fail()
    want = _text(args, "want", limit=200)
    count = _int(args, "count", default=1, low=1, high=5)
    chat = [str(line)[:200] for line in (args.get("recent_chat") or []) if isinstance(line, str)][-20:]
    in_room = bool(ctx.group_id and service.group_allowed(ctx.group_id) and service.hub.get(ctx.group_id))
    taste = service.library.summary_for_recommend(ctx.user_id) if service.library else []
    played = service.played_recently(ctx.group_id) if ctx.group_id else []
    req = RecommendRequest(want=want, count=count, chat=chat, played=played, taste=taste)
    try:
        tracks = await recommend(req, service.sources, playable=service.playable)
    except Exception as e:
        raise ToolError(f"选歌失败：{describe_exception('推荐', e)}") from None
    if not tracks:
        raise ToolError("想了几首都没在平台上找到，换个说法再试")
    service.remember(tracks)
    names = "、".join(t.label() for t in tracks)

    if in_room:
        added, skipped = [], []
        for track in tracks:
            if not service.playable(track):
                replacement = await service.substitute(track)
                if replacement is None:
                    skipped.append(track.label())
                    continue
                track = replacement
            if await _add_to_room(service, ctx, track):
                added.append(track.label())
        if not added:
            raise ToolError(f"选了{names}，但都是会员歌，放不了")
        note = "（" + "、".join(skipped) + " 是会员歌放不了，没加）" if skipped else ""
        return {"content": f"已经把 {'、'.join(added)} 加进本群一起听的队列了{note}。"
                           "用你自己的口吻简短介绍一下为什么选这首，不要发链接。"}

    async def job():
        for track in tracks:
            try:
                await _send_card(ctx, track)
            except Exception as e:
                logger.opt(exception=e).warning("[凛音工具] music_recommend 发卡片失败")
    _spawn("music_recommend", job())
    return {"content": f"选好了：{names}，卡片马上发出去。用你自己的口吻简短介绍一下为什么选，不要再报一遍歌名。"}


async def _add_to_room(service: Any, ctx: ToolContext, track: Any) -> bool:
    from ..listen_plugin.room import RoomError

    room = service.hub.get(ctx.group_id)
    if room is None:
        return False
    name = "凛音"
    try:
        room.add(track, "rinne", name)
        if room.current is None:
            room.next("rinne", service.clock(), superuser=True)
            await service.hub.push_state(ctx.group_id)
    except RoomError as e:
        logger.info("[凛音工具] 加进队列失败：{}", e)
        return False
    service.hub.save(ctx.group_id)
    await service.hub.push_queue(ctx.group_id)
    who = ctx.user_name or ctx.user_id
    await service.hub.broadcast(ctx.group_id, {
        "type": "chat", "system": True, "ts": service.clock(),
        "text": f"凛音给{who}推荐了 {track.label()}，加进队列了",
    })
    return True


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
        ToolSpec("listen_context", "本群正在一起听什么（回复前注入用）", {}, tool_listen_context),
        ToolSpec("listen_status", "本群一起听的队列和刚放过的歌", {}, tool_listen_status),
        ToolSpec("music_share", "发一首歌的音乐卡片", {"song": {"type": "string", "required": True}},
                 tool_music_share),
        ToolSpec(
            "music_recommend",
            "按要求推荐歌",
            {
                "want": {"type": "string"},
                "count": {"type": "integer"},
                "recent_chat": {"type": "array"},
            },
            tool_music_recommend,
        ),
    )
}
