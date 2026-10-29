"""一起听：白名单群的群友在独立域名的网页上同步听歌。

一起听的网页服务和 NoneBot 跑在同一个进程里，但监听自己的端口（LISTEN_PORT，默认 8090），
不复用 NoneBot 的 FastAPI，也不复用 WebUI 的登录。配置见 config.py，设计见 DESIGN.md。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from nonebot import get_bot, get_driver, logger, on_notice, on_regex, require

require("nonebot_plugin_localstore")

from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import ActionFailed, GroupDecreaseNoticeEvent
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata

from .._shared.events import group_id_of
from .._shared.help import HelpTopic, help_registry
from .auth import AuthError
from .bridge import BotUnavailable, Member, Profile
from .config import Deployment, listen_settings, load_deployment
from .service import ListenConfig, ListenService
from .sources.base import MusicSource
from .sources.kugou import KugouSource
from .sources.netease import NeteaseSource
from .sources.qqmusic import QQMusicSource
from .store import Store

__plugin_meta__ = PluginMetadata(
    name="listen",
    description="一起听",
    usage="在白名单群里发 /一起听",
    type="application",
    supported_adapters={"~onebot.v11"},
)


def _allowed_groups() -> list[str]:
    return [g for g in listen_settings.allowed_groups if g.isdigit()]


class NoneBotBridge:
    async def member(self, group_id: str, qq: str) -> Member | None:
        try:
            bot = get_bot()
        except ValueError:
            raise BotUnavailable from None
        try:
            info = await bot.call_api(
                "get_group_member_info", group_id=int(group_id), user_id=int(qq), no_cache=False
            )
        except ActionFailed:
            return None  # 不在群里
        except Exception as e:
            raise BotUnavailable from e
        if not info:
            return None
        return Member(
            qq=qq,
            group_id=group_id,
            nickname=str(info.get("nickname") or ""),
            card=str(info.get("card") or ""),
            title=str(info.get("title") or ""),
            level=str(info.get("level") or ""),
            role=str(info.get("role") or "member"),
        )

    async def profile(self, qq: str) -> Profile:
        try:
            info = await get_bot().call_api("get_stranger_info", user_id=int(qq), no_cache=False)
        except Exception:  # noqa: BLE001 资料卡的附加信息拿不到就不显示
            return Profile()
        info = info or {}
        # NapCat 返回 long_nick 作为个性签名，qqLevel 是 QQ 等级。
        return Profile(
            signature=str(info.get("long_nick") or info.get("sign") or "")[:200],
            qq_level=int(info.get("qqLevel") or info.get("level") or 0),
            sex=str(info.get("sex") or ""),
        )

    def is_superuser(self, qq: str) -> bool:
        superusers = get_driver().config.superusers
        return qq in superusers or f"onebot:{qq}" in superusers


def _data_dir() -> Path:
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:  # noqa: BLE001 和其他插件一样：localstore 不可用时退回工作目录
        fallback = Path.cwd() / "data" / "listen"
        logger.warning(f"[一起听] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


def _sources(dep: Deployment) -> dict[str, MusicSource]:
    found: dict[str, MusicSource] = {"qq": QQMusicSource()}
    if dep.netease_api:
        found["netease"] = NeteaseSource(dep.netease_api)
    if dep.kugou_api:
        found["kugou"] = KugouSource(dep.kugou_api)
    missing = [name for name, url in (("NETEASE_API_URL", dep.netease_api), ("KUGOU_API_URL", dep.kugou_api)) if not url]
    if missing:
        logger.info("[一起听] 没有设置 {}，对应的平台不接入", "、".join(missing))
    return found


deployment, disabled_reason = load_deployment()
service: ListenService | None = None
_server_task: asyncio.Task | None = None
_server = None
_background: set[asyncio.Task] = set()

if deployment is not None:
    service = ListenService(
        config=ListenConfig(
            public_url=deployment.public_url,
            secret=deployment.secret,
            allowed_groups=_allowed_groups,
            trusted_proxies=deployment.trusted_proxies,
            max_members=lambda: int(listen_settings.max_members),
            bitrate=lambda: int(listen_settings.bitrate),
            downgrade_over=lambda: int(listen_settings.downgrade_over),
        ),
        bridge=NoneBotBridge(),
        store=Store(_data_dir() / "listen.db"),
        source_map=_sources(deployment),
    )
else:
    logger.warning("[一起听] 未启用：{}", disabled_reason)


driver = get_driver()


@driver.on_startup
async def _start() -> None:
    global _server, _server_task
    if service is None or deployment is None:
        return
    host, port = deployment.host, deployment.port
    import uvicorn

    from .app import create_app

    config = uvicorn.Config(
        create_app(service),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,  # 真实 IP 由 app.client_ip 按 LISTEN_TRUSTED_PROXIES 判断
        ws_max_size=64 * 1024,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    # 不让第二个 uvicorn 接管 Ctrl+C / SIGTERM，停止由 NoneBot 的 shutdown 钩子负责。
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    async def run() -> None:
        try:
            await server.serve()
        except SystemExit:
            # uvicorn 绑定端口失败时会 sys.exit(1)，这里拦下来，不影响 bot 其他功能。
            logger.error("[一起听] 端口 {}:{} 无法监听（可能被占用），一起听没有启动", host, port)
        except Exception as e:  # noqa: BLE001
            logger.opt(exception=e).error("[一起听] 服务异常退出")

    async def watch_whitelist() -> None:
        # WebUI 里把群移出白名单后，一分钟内关掉房间、作废登录（新请求本来就会被拒，这里处理已连着的人）。
        while service is not None:
            await asyncio.sleep(60)
            try:
                await service.enforce_whitelist()
            except Exception as e:  # noqa: BLE001
                logger.opt(exception=e).warning("[一起听] 检查白名单失败")

    _server = server
    loop = asyncio.get_running_loop()
    _server_task = loop.create_task(run())
    _background.add(loop.create_task(watch_whitelist()))
    service.hub.start()
    await service.enforce_whitelist()
    logger.info("[一起听] 已启动：{}（监听 {}:{}）", deployment.public_url, deployment.host, deployment.port)


@driver.on_shutdown
async def _stop() -> None:
    for task in _background:
        task.cancel()
    if service is not None:
        await service.hub.stop()
        for source in service.sources.values():
            close = getattr(source, "close", None)
            if close:
                with contextlib.suppress(Exception):
                    await close()
    if _server is not None:
        _server.should_exit = True
    if _server_task is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(_server_task, timeout=5)
    if service is not None:
        service.store.close()


# ---------------------------------------------------------------- 群指令

listen_cmd = on_regex(r"^/一起听(?:\s+(\S+)(?:\s+(\S+))?)?\s*$", priority=5, block=True)


@listen_cmd.handle()
async def handle_listen(bot: Bot, event: Event, matcher: Matcher, groups: tuple = RegexGroup()):
    if service is None:
        await matcher.finish()
    sub, arg = (groups[0] or "").strip(), (groups[1] or "").strip()
    qq = event.get_user_id()
    group_id = group_id_of(event)

    if sub == "登录":
        await _redeem(matcher, qq, group_id, arg)
        return

    if group_id is None:
        await matcher.finish("在开了一起听的群里发 /一起听。")
    if not service.group_allowed(group_id):
        await matcher.finish()  # 不在白名单的群里不回应

    superuser = service.is_superuser(qq)
    if sub == "关闭":
        room = service.hub.get(group_id)
        if room is None:
            await matcher.finish("现在没有一起听。")
        if qq != room.owner and not superuser:
            await matcher.finish("只有房主或管理员能关。")
        await service.hub.close_room(group_id)
        await matcher.finish("一起听关了。")
    if sub:
        await matcher.finish("用法：/一起听、/一起听 关闭、/一起听 登录 验证码")

    room, created = service.hub.open(group_id, qq)
    if created:
        service.hub.reschedule(group_id)
    url = service.login_url(qq, group_id)
    try:
        await bot.call_api(
            "send_private_msg",
            user_id=int(qq),
            message=f"一起听的登录链接（10 分钟内有效，只能用一次，别发给别人）：\n{url}",
        )
    except Exception as e:  # noqa: BLE001 没加好友、临时会话关了都会失败
        logger.info("[一起听] 私聊 {} 失败：{}", qq, e)
        await matcher.finish(
            f"{'一起听开好了。' if created else ''}私聊发不出去，"
            f"打开 {service.config.public_url} 用验证码登录。"
        )
    await matcher.finish(f"{'一起听开好了，' if created else ''}登录链接私聊发给你了。")


async def _redeem(matcher: Matcher, qq: str, group_id: str | None, code: str) -> None:
    if service is None:
        await matcher.finish()
        return
    if not (code.isdigit() and len(code) == 6):
        await matcher.finish("验证码是网页上显示的 6 位数字：/一起听 登录 123456")
    # 私聊发码时，按这个人所在的白名单群登录；在多个群里时选当前开着房间的那个。
    try:
        if group_id is not None:
            if not service.group_allowed(group_id):
                await matcher.finish()
            candidates = [group_id]
        else:
            candidates = await service.groups_of(qq)
    except BotUnavailable:
        await matcher.finish("现在查不了群成员，稍后再试。")
    if not candidates:
        await matcher.finish("你不在开放一起听的群里。")
    open_rooms = [g for g in candidates if service.hub.get(g)]
    target = (open_rooms or candidates)[0]
    try:
        ok = service.redeem_code(code, qq, target)
    except AuthError as e:
        await matcher.finish(str(e))
    await matcher.finish("登录好了，回网页看看。" if ok else "验证码不对或者过期了，网页上点「换一个」再试。")


# 退群、被踢：立即踢出房间，登录作废。
leave_notice = on_notice(priority=5, block=False)


@leave_notice.handle()
async def handle_leave(event: Event):
    if service is None or not isinstance(event, GroupDecreaseNoticeEvent):
        return
    await service.member_left(str(event.group_id), str(event.user_id))


help_registry.register(
    HelpTopic(
        key="listen",
        title="一起听",
        aliases=("听歌", "一起听歌"),
        summary=("/一起听 - 开一起听，登录链接私聊发给你",),
        detail=(
            "/一起听 - 没有房间就开一个，登录链接私聊发给你（10 分钟内有效，只能用一次）\n"
            "/一起听 登录 123456 - 私聊发不出去时，打开网页拿验证码，把码发给凛音\n"
            "/一起听 关闭 - 关掉房间（房主或管理员）\n\n"
            "网页上大家可以一起点歌、切歌、暂停、拖进度，房主可以关掉大家控制播放的权限。"
        ),
        order=40,
        available=lambda: service is not None and bool(_allowed_groups()),
    )
)

__all__ = ["listen_settings", "service"]
