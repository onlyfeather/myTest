"""关注作者更新推送：后台定时检查，有新作品发到 WebUI 里设置的群。"""

import asyncio
import contextlib
import time

from nonebot import get_bots, get_driver, logger
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import Target, UniMessage

from ..._shared.settings import ItemManager, ManagerAction, settings_store
from ..._shared.status import StatusRow, ago, status_registry
from ..pixiv.errors import describe
from ..services import follow_manage
from ..common import pixiv_command, report_error
from ..config import pixiv_settings
from ..pixiv import get_pixiv_spider
from ..renderers import send_images_with_info
from ..services.follow_push import check_author, skipped_note, update_title

FIRST_DELAY = 120  # 启动后等一会儿再查，避开 NapCat 刚连上的那段时间
AUTHOR_GAP = 3.0  # 两个作者之间隔几秒，免得触发 Pixiv 风控

_task: asyncio.Task | None = None
_lock = asyncio.Lock()
_last_run: dict[str, float | dict[str, int]] = {}


def pixiv_status() -> list[StatusRow]:
    from ..pixiv.client import _spider

    rows = []
    if _spider is None:
        rows.append(StatusRow("登录", "还没有用过 Pixiv 功能，没有加载"))
    else:
        rows.append(StatusRow("登录", "已填 Cookie" if _spider.is_logged_in else "没有 Cookie：在「Pixiv」里填写",
                              "ok" if _spider.is_logged_in else "error"))
        reason = _spider.last_error_reason
        if reason and _spider.is_logged_in:
            rows.append(StatusRow("最近的错误", describe(reason), "warn"))
        rows.append(StatusRow("关注的作者", f"{len(_spider.favorite_authors)} 个"))
    if not pixiv_settings.push_enabled:
        rows.append(StatusRow("关注推送", "没有开启"))
    elif not push_groups():
        rows.append(StatusRow("关注推送", "已开启，但没有填推送的群", "warn"))
    else:
        at = float(_last_run.get("at") or 0)
        stats = _last_run.get("stats") or {}
        text = f"推送到 {len(push_groups())} 个群，每 {int(pixiv_settings.push_interval)} 分钟检查；上次检查：{ago(at)}"
        if at and isinstance(stats, dict):
            text += f"，推送了 {stats.get('pushed', 0)} 张"
        rows.append(StatusRow("关注推送", text, "ok"))
    return rows


status_registry.register("pixiv", "Pixiv", pixiv_status, order=50)


async def _manager_items() -> dict:
    async with get_pixiv_spider() as spider:
        return follow_manage.list_items(spider)


async def _manager_run(action: str, item_id: str, value) -> str:
    if action == "check":
        if not push_groups():
            raise ValueError("还没有在上面填「推送到这些群」")
        if _lock.locked():
            raise ValueError("正在检查中，稍等")
        stats = await run_once()
        text = f"检查完了：{stats['authors']} 个作者，推送了 {stats['pushed']} 张"
        if stats["first"]:
            text += f"，{stats['first']} 个作者是第一次检查（只记下最新作品）"
        return text
    async with get_pixiv_spider() as spider:
        return await follow_manage.run_action(spider, action, item_id, value)


settings_store.set_manager("pixiv", ItemManager(
    title="关注的作者",
    help="关注推送会检查这里的作者。可以填作者 ID 或主页链接添加，也可以一次导入登录 Cookie 对应的 Pixiv 账号关注的人。"
         "群里的「p站 关注」「p站 取关」改的也是这份列表。",
    items=_manager_items,
    actions=(
        ManagerAction("add", "添加作者", scope="global", input="lines", primary=True,
                      placeholder="每行一个作者 ID 或主页链接\n例：https://www.pixiv.net/users/123456"),
        ManagerAction("import", "从我的 Pixiv 关注导入", scope="global",
                      confirm="把 Cookie 对应的 Pixiv 账号关注的作者（含非公开关注）全部加进来？已有的不会重复"),
        ManagerAction("check", "马上检查更新", scope="global",
                      confirm="现在检查一遍所有作者？有新作品会马上发到群里"),
        ManagerAction("aliases", "圈名", input="lines", placeholder="每行一个圈名，群里可以用圈名代替 ID"),
        ManagerAction("reset_push", "重新开始推送", confirm="「{title}」下次检查时只记下最新作品、不推送旧作。确定吗？"),
        ManagerAction("remove", "取关", confirm="取关「{title}」？"),
    ),
    run=_manager_run,
))


def push_groups() -> list[str]:
    return [g for g in (str(x).strip() for x in pixiv_settings.push_groups) if g.isdigit()]


async def run_once() -> dict[str, int]:
    """检查一轮，返回 {authors, pushed, first}。同一时间只跑一轮。"""
    stats = {"authors": 0, "pushed": 0, "first": 0}
    groups = push_groups()
    if not groups:
        return stats
    if not get_bots():
        logger.info("[Pixiv推送] QQ 机器人不在线，这一轮跳过")
        return stats
    async with _lock:
        async with get_pixiv_spider() as spider:
            if not spider.is_logged_in:
                logger.info("[Pixiv推送] 还没有登录 Pixiv，跳过")
                return stats
            for index, user_id in enumerate(list(spider.favorite_authors)):
                if index:
                    await asyncio.sleep(AUTHOR_GAP)
                try:
                    update = await check_author(
                        spider,
                        user_id,
                        max_images=int(pixiv_settings.push_max_per_author),
                        allow_r18=bool(pixiv_settings.push_allow_r18),
                    )
                except Exception as e:  # 一个作者出错不影响其他作者
                    logger.opt(exception=e).warning("[Pixiv推送] 检查作者 {} 失败", user_id)
                    continue
                if update is None:
                    continue
                stats["authors"] += 1
                spider.update_author_last_check(user_id)
                if update.first_check:
                    stats["first"] += 1
                    continue
                if not update.images:
                    continue
                for group_id in groups:
                    target = Target.group(group_id)
                    try:
                        await send_images_with_info(update.images, update_title(update), target=target)
                        if update.skipped:
                            await UniMessage.text(skipped_note(update)).send(target=target)
                    except Exception as e:
                        logger.opt(exception=e).warning("[Pixiv推送] 发到群 {} 失败", group_id)
                stats["pushed"] += len(update.images)
    _last_run.update(at=time.time(), stats=stats)
    logger.info("[Pixiv推送] 检查了 {authors} 个作者，推送 {pushed} 张，首次记录 {first} 个", **stats)
    return stats


async def _loop() -> None:
    await asyncio.sleep(FIRST_DELAY)
    while True:
        if pixiv_settings.push_enabled:
            try:
                await run_once()
            except Exception as e:  # 后台任务不能因为一次出错就停掉
                logger.opt(exception=e).warning("[Pixiv推送] 检查出错")
        # 每轮重新读取间隔，WebUI 里改了下一轮生效
        await asyncio.sleep(max(15, int(pixiv_settings.push_interval)) * 60)


driver = get_driver()


@driver.on_startup
async def _start() -> None:
    global _task
    _task = asyncio.get_running_loop().create_task(_loop())


@driver.on_shutdown
async def _stop() -> None:
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task


push_check = Alconna(pixiv_command("检查更新"))
push_check_matcher = on_alconna(push_check, use_cmd_start=True, permission=SUPERUSER, block=True)


@push_check_matcher.handle()
async def push_check_handle(result: Arparma):
    if not push_groups():
        await UniMessage.text("还没有设置推送的群：在 /webui 的「Pixiv」里填「推送到这些群」").send()
        return
    if _lock.locked():
        await UniMessage.text("正在检查中，稍等").send()
        return
    await UniMessage.text("开始检查关注作者的更新，作者多的话要几分钟").send()
    try:
        stats = await run_once()
    except Exception as e:
        await report_error("检查关注作者更新", e)
        return
    text = f"检查完了：{stats['authors']} 个作者，推送了 {stats['pushed']} 张"
    if stats["first"]:
        text += f"，{stats['first']} 个作者是第一次检查（只记下最新作品，下次有新的才推送）"
    await UniMessage.text(text).send()


__all__ = ["push_check_matcher", "run_once"]
