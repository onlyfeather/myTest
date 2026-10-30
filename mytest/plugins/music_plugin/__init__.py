"""点歌：从 QQ 音乐、网易云、酷狗三个平台搜歌，回复编号发音乐卡片。凛音的发歌、推荐歌工具也用这里的音源。

bot 的会员账号在 WebUI「点歌」里扫码登录，会员歌的卡片可以完整播放。配置见 config.py。
"""

from __future__ import annotations

from pathlib import Path

from nonebot import get_driver, logger, on_message, on_regex, require

require("nonebot_plugin_localstore")

from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .._shared.events import group_id_of, is_superuser
from .._shared.help import HelpTopic, help_registry
from .._shared.limits import UsageLimiter, scope_allowed
from .._shared.settings import AccountPanel, settings_store
from .._shared.status import StatusRow, ago, status_registry
from .accounts.base import PlatformAccounts
from .accounts.kugou import KugouAccounts
from .accounts.netease import NeteaseAccounts
from .accounts.qqmusic import QQMusicAccounts
from .card import build_card
from .config import api_url, credential_keys, music_settings
from .crypto import CryptoError, Vault
from .picker import Picker, format_list, number_choices
from .service import MusicService
from .sources.base import PLATFORM_NAMES, MusicSource, Track
from .sources.kugou import KugouSource
from .sources.netease import NeteaseSource
from .sources.qqmusic import QQMusicSource
from .store import Store
from .vip import VipAccounts, VipError

__plugin_meta__ = PluginMetadata(
    name="music",
    description="点歌",
    usage="/点歌 歌名，回复编号发音乐卡片",
    type="application",
    supported_adapters={"~onebot.v11"},
)


def _data_dir() -> Path:
    try:
        import nonebot_plugin_localstore as localstore

        # 一起听时期的数据目录叫 listen_plugin，里面的 listen.db 存着 bot 的会员账号。有就接着用，不用重新扫码。
        new = localstore.get_plugin_data_dir()
        old = new.parent / "listen_plugin"
        if (old / "listen.db").exists() and not (new / "music.db").exists():
            return old
        return new
    except Exception as e:  # noqa: BLE001 和其他插件一样：localstore 不可用时退回工作目录
        fallback = Path.cwd() / "data" / "music"
        logger.warning(f"[点歌] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


def _db_path() -> Path:
    folder = _data_dir()
    old = folder / "listen.db"
    return old if old.exists() else folder / "music.db"


def _sources() -> dict[str, MusicSource]:
    netease, kugou = api_url("netease_api_url"), api_url("kugou_api_url")
    found: dict[str, MusicSource] = {"qq": QQMusicSource()}
    if netease:
        found["netease"] = NeteaseSource(netease)
    if kugou:
        found["kugou"] = KugouSource(kugou)
    missing = [name for name, url in (("NETEASE_API_URL", netease), ("KUGOU_API_URL", kugou)) if not url]
    if missing:
        logger.warning("[点歌] 没有设置 {}，这些平台搜不到歌", "、".join(missing))
    return found


def _vip(store: Store) -> VipAccounts:
    accounts: dict[str, PlatformAccounts] = {"qq": QQMusicAccounts()}
    if api_url("netease_api_url"):
        accounts["netease"] = NeteaseAccounts(api_url("netease_api_url"))
    if api_url("kugou_api_url"):
        accounts["kugou"] = KugouAccounts(api_url("kugou_api_url"))
    current, old = credential_keys()
    try:
        vault = Vault.from_env(current, old)
    except CryptoError as e:
        logger.error("[点歌] {}：bot 会员账号不可用", e)
        vault = None
    if vault is None and not current:
        logger.info("[点歌] 没有设置 MUSIC_CREDENTIAL_KEY：bot 会员账号不可用，会员歌只能试听")
    return VipAccounts(store, accounts, vault)


def _register_vip_panel(vip: VipAccounts) -> None:
    """WebUI「点歌」里的 bot 会员账号：扫码登录后，会员歌的卡片可以完整播放。"""

    async def qr_start(platform: str, kind: str) -> dict:
        try:
            sid, start = await vip.qr_start(platform, kind)
        except VipError as e:
            raise ValueError(str(e)) from e
        return {"sid": sid, "image": start.image, "hint": start.hint}

    async def qr_poll(sid: str) -> dict:
        try:
            return await vip.qr_poll(sid)
        except VipError as e:
            raise ValueError(str(e)) from e

    settings_store.set_account_panel("music", AccountPanel(
        title="bot 的会员账号",
        help=(
            "用来发会员歌：卡片里点播放能听完整版。三个平台各登录一个会员账号（建议用小号）。"
            "登录信息加密保存，每 12 小时自动续一次；失效了这里会显示，重新扫码就行。"
            + ("" if vip.vault else "\n还没有设置 MUSIC_CREDENTIAL_KEY，现在不能登录。")
        ),
        status=vip.status,
        qr_start=qr_start,
        qr_poll=qr_poll,
        logout=vip.logout,
    ))


def music_status(service: MusicService, vip: VipAccounts) -> list[StatusRow]:
    rows = []
    accounts = {item["platform"]: item for item in vip.status()}
    for platform in service.platforms():
        name = PLATFORM_NAMES.get(platform, platform)
        failures, wait = service.sources.health(platform)
        if wait:
            rows.append(StatusRow(name, f"连续出错，暂停使用，{int(wait // 60) + 1} 分钟后恢复", "error"))
        else:
            rows.append(StatusRow(name, f"正常（最近连续失败 {failures} 次）" if failures else "正常",
                                  "warn" if failures else "ok"))
        acc = accounts.get(platform)
        if acc is None:
            continue
        if acc["logged_in"]:
            text = f"{acc['account_name'] or '已登录'}{' · 会员' if acc['vip'] else ' · 不是会员'}"
            text += f"，{ago(acc['refreshed_at'] / 1000)}续期" if acc["refreshed_at"] else ""
            rows.append(StatusRow(f"{name}会员账号", text, "ok" if acc["vip"] else "warn"))
        elif acc["expired"]:
            rows.append(StatusRow(f"{name}会员账号", f"已失效：{acc['message'] or '需要重新扫码'}", "error"))
        else:
            rows.append(StatusRow(f"{name}会员账号", "未登录，会员歌只能试听", "warn"))
    missing = [PLATFORM_NAMES[p] for p in ("netease", "kugou") if p not in service.sources]
    if missing:
        rows.append(StatusRow("没有接入", "、".join(missing) + "（没有设置接口地址）", "warn"))
    return rows


_store = Store(_db_path())
vip_accounts = _vip(_store)
music = MusicService(_sources(), vip_accounts)
picks = Picker()
limiter = UsageLimiter()
_register_vip_panel(vip_accounts)
status_registry.register("music", "点歌", lambda: music_status(music, vip_accounts), order=40)

driver = get_driver()


@driver.on_startup
async def _start() -> None:
    vip_accounts.start()


@driver.on_shutdown
async def _stop() -> None:
    await vip_accounts.stop()
    for source in music.source_map.values():
        close = getattr(source, "close", None)
        if close:
            try:
                await close()
            except Exception:  # noqa: BLE001,S110 关闭时出错不影响退出
                pass
    _store.close()


# ---------------------------------------------------------------- 发卡片


async def send_card(bot: Bot, event: Event, track: Track) -> None:
    """发音乐卡片。签名服务挂了、NapCat 不认时退回「封面 + 歌名 + 链接」。"""
    card = build_card(track, await music.card_audio(track))
    try:
        # NapCat：不带 id 的 music 段按自定义卡片签名，type 填平台决定角标（见 card.py）
        await bot.send(event, Message(MessageSegment("music", card.segment_data())))
    except Exception as e:  # noqa: BLE001
        logger.info("[点歌] 音乐卡片发送失败，改发文字：{}", e)
        msg = Message(MessageSegment.text(card.fallback_text()))
        if card.image:
            msg = Message(MessageSegment.image(card.image)) + msg
        await bot.send(event, msg)


def _scope(event: Event) -> str:
    group_id = group_id_of(event)
    return f"group:{group_id}" if group_id else f"private:{event.get_user_id()}"


def _usable(event: Event) -> bool:
    return bool(music_settings.enabled) and scope_allowed(
        group_id_of(event),
        allowed_groups=list(music_settings.allowed_groups),
        allow_private=bool(music_settings.allow_private),
    )


# ---------------------------------------------------------------- /点歌


song_cmd = on_regex(r"^/点歌(?:\s+([\s\S]*))?$", priority=5, block=True)


@song_cmd.handle()
async def handle_song(bot: Bot, event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    if not _usable(event):
        await matcher.finish()
    keyword = (groups[0] or "").strip()[:60]
    if not keyword:
        await matcher.finish("用法：/点歌 歌名（最好带上歌手，例如 /点歌 晴天 周杰伦）")

    user_id = event.get_user_id()
    reason = await limiter.try_acquire(
        user_id,
        cooldown=float(music_settings.cooldown),
        max_concurrent=5,
        exempt=await is_superuser(bot, event),
    )
    if reason:
        await matcher.finish(reason)

    success = False
    try:
        results = await music.search_all(keyword, int(music_settings.per_platform))
        success = any(r.tracks for r in results)
    finally:
        limiter.release(user_id, success=success)

    scope = _scope(event)
    picks.cancel(scope, user_id)
    if not success:
        errors = "；".join(f"{PLATFORM_NAMES.get(r.platform, r.platform)}：{r.error}" for r in results if r.error)
        await matcher.finish(f"三个平台都没搜到「{keyword}」" + (f"（{errors}）" if errors else "") + "，换个关键词试试。")

    grouped = [(r.platform, r.tracks) for r in results]
    choices = number_choices(grouped)
    errors = {r.platform: r.error for r in results if r.error}
    picks.offer(scope, user_id, choices)
    await matcher.finish(format_list(keyword, grouped, errors, choices, vip_ready=music.vip_ready))


# ---------------------------------------------------------------- 回复编号


async def _is_picking(event: Event) -> bool:
    if not music_settings.enabled:
        return False
    return picks.waiting(_scope(event), event.get_user_id()) and event.get_plaintext().strip().isdigit()


pick_reply = on_message(rule=Rule(_is_picking), priority=4, block=True)


@pick_reply.handle()
async def handle_pick(bot: Bot, event: Event, matcher: Matcher):
    picked = picks.take(_scope(event), event.get_user_id(), event.get_plaintext())
    if picked is None:
        await matcher.finish()
    if isinstance(picked, str):
        await matcher.finish(picked)
    try:
        await send_card(bot, event, picked)
    except Exception as e:  # noqa: BLE001
        logger.opt(exception=e).warning("[点歌] 发卡片失败")
        await matcher.finish("歌没发出去，稍后再试。")


help_registry.register(
    HelpTopic(
        key="music",
        title="点歌",
        aliases=("听歌", "音乐"),
        summary=("/点歌 歌名 - 三个平台搜歌，回复编号发音乐卡片",),
        detail=lambda: (
            "/点歌 歌名 - 从 QQ 音乐、网易云、酷狗搜歌，每个平台列几首，回复编号发音乐卡片\n"
            "  例：/点歌 晴天 周杰伦，然后回复 2\n"
            "  2 分钟内有效，回复 0 取消\n"
            "卡片里点播放就能听。标着 [会员·只能试听] 的歌，bot 还没在那个平台登录会员账号。\n\n"
            "也可以直接跟凛音说：「发一下晴天」「推荐首安静的歌」"
        ),
        admin=("bot 的会员账号在网页 /webui 的「点歌」里扫码登录",),
        order=40,
        available=lambda: bool(music_settings.enabled),
    )
)

__all__ = ["music", "music_settings", "send_card"]
