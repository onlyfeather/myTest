from nonebot import logger, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import pixiv_command, report_error
from ..pixiv import get_pixiv_spider

WEBUI_HINT = "Pixiv 登录 Cookie 改到网页里设置了：打开 /webui →「Pixiv」→「登录 Cookie」，保存后点「测试连接」。"


async def private_superuser(bot: Bot, event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent) and await SUPERUSER(bot, event)


login_status = Alconna(pixiv_command("登录状态"))

quality_setting = Alconna(
    pixiv_command("质量设置"),
    Args["score?", int],
)

# 旧指令保留：不再接受 Cookie，只提示去 WebUI。带着 Cookie 发过来也不保存、不回显。
login_matcher = on_regex(
    r"^/?p站\s+登录(?:\s+.*)?$",
    permission=private_superuser,
    priority=1,
    block=True,
)
login_status_matcher = on_alconna(
    login_status,
    use_cmd_start=True,
    permission=private_superuser,
    block=True,
)
quality_matcher = on_alconna(
    quality_setting,
    use_cmd_start=True,
    permission=private_superuser,
    block=True,
)


@login_matcher.handle()
async def login_handle():
    await UniMessage.text(WEBUI_HINT + "\n这条消息里的 Cookie 没有保存，建议撤回。").send()


@login_status_matcher.handle()
async def login_status_handle(result: Arparma):
    try:
        async with get_pixiv_spider() as spider:
            if not spider.is_logged_in:
                await UniMessage.text("❌ 还没有设置 Pixiv Cookie。" + WEBUI_HINT).send()
                return
            try:
                message = await spider.check_login()
            except Exception as e:
                message = f"Cookie 检查没通过：{e}"
            await UniMessage.text(message).send()
    except Exception as e:
        await report_error("检查登录状态时发生错误", e)


@quality_matcher.handle()
async def quality_setting_handle(result: Arparma):
    score = result.query[int]("score")

    try:
        async with get_pixiv_spider() as spider:
            if score is None:
                current = int(spider.min_quality_score)
                await UniMessage.text(
                    f"当前质量评分阈值：{current}分\n用法：/p站 质量设置 [0-100]"
                ).send()
                return
            saved = spider.set_min_quality_score(score)
        await UniMessage.text(
            f"✅ 质量评分阈值已设置为：{saved}分\n"
            "💡 随机模式会优先选择不低于此分数的作品，找不到时才降级"
        ).send()
    except Exception:
        logger.exception("[Pixiv] 设置质量评分失败")
        await UniMessage.text("设置质量评分时发生错误，请查看后台日志").send()


__all__ = [
    "login_matcher",
    "login_status_matcher",
    "quality_matcher",
]
