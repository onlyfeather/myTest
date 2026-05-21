from nonebot import on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.params import RegexStr
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import PIXIV_PREFIX, extract_plain_text, pixiv_command
from ..pixiv import get_pixiv_spider


async def private_superuser(bot: Bot, event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent) and await SUPERUSER(bot, event)


login_status = Alconna(pixiv_command("登录状态"))

quality_setting = Alconna(
    pixiv_command("质量设置"),
    Args["score", int, 60],
)

login_matcher = on_regex(
    r"^/p站\s+登录(?:\s+.*)?$",
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
async def login_handle(event: Event, regex_str: str = RegexStr()):
    text = (regex_str or extract_plain_text(event)).strip()
    prefix = f"/{PIXIV_PREFIX} 登录"
    cookie = text[len(prefix) :].strip() if text.startswith(prefix) else ""

    if not cookie:
        await UniMessage.text(f"请提供Cookie，例如：/{PIXIV_PREFIX} 登录 your_cookie_string").send()
        return

    try:
        async with get_pixiv_spider() as spider:
            if spider.set_cookie(cookie):
                await UniMessage.text("✅ 登录成功！Pixiv功能已启用").send()
            else:
                await UniMessage.text("❌ 登录失败，请检查Cookie是否正确").send()
    except Exception as e:
        await UniMessage.text(f"登录时发生错误：{str(e)}").send()


@login_status_matcher.handle()
async def login_status_handle(result: Arparma):
    try:
        async with get_pixiv_spider() as spider:
            cookie_data = spider.storage.load_cookie()
            if spider.is_logged_in and cookie_data:
                user_id = spider.user_id or cookie_data.get("user_id")
                saved_time = cookie_data.get("saved_time") or "未知"
                cookie_text = cookie_data.get("full_cookie") or ""
                message = "✅ p站 登录设置\n\n"
                message += "状态：已配置 Cookie\n"
                message += f"用户ID：{user_id or '未记录'}\n"
                message += f"保存时间：{saved_time}\n"
                message += f"Cookie长度：{len(cookie_text)} 字符\n\n"
                message += "提示：这里只表示本地已保存 Cookie，实际有效性以请求 p站 成功为准。"
                await UniMessage.text(message).send()
            else:
                await UniMessage.text("❌ 未登录，请使用「/p站 登录」命令设置Cookie").send()
    except Exception as e:
        await UniMessage.text(f"检查登录状态时发生错误：{str(e)}").send()


@quality_matcher.handle()
async def quality_setting_handle(result: Arparma):
    score = result.query[int]("score")
    score = min(max(score or 0, 0), 100)

    try:
        await UniMessage.text(f"✅ 质量评分阈值已设置为：{score}分").send()
        await UniMessage.text("💡 低于此分数的图片在随机模式下将被降级处理").send()
    except Exception as e:
        await UniMessage.text(f"设置质量评分时发生错误：{str(e)}").send()


__all__ = [
    "login_matcher",
    "login_status_matcher",
    "quality_matcher",
]
