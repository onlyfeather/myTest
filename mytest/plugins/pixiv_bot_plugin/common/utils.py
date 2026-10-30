from nonebot import logger
from nonebot.adapters import Event
from nonebot_plugin_alconna.uniseg import UniMessage

from .constants import PIXIV_PREFIX


async def report_error(action: str, exc: Exception) -> None:
    """记录完整异常堆栈，并给用户发送一条简短提示。"""
    logger.opt(exception=exc).error(f"[Pixiv] {action}时发生错误")
    try:
        await UniMessage.text(f"{action}时发生错误：{type(exc).__name__}，详情见后台日志").send()
    except Exception:
        logger.exception("[Pixiv] 发送错误提示失败")


def pixiv_command(name: str) -> str:
    return f"{PIXIV_PREFIX} {name}"


def extract_plain_text(event: Event) -> str:
    if hasattr(event, "get_plaintext"):
        return event.get_plaintext()
    return event.get_plain_text()


def pixiv_failure_message(
    spider,
    action: str,
    target: str,
    detail: str = "",
) -> str:
    from ..pixiv.errors import describe

    message = f"{action}「{target}」没有返回图片。\n原因：{describe(getattr(spider, 'last_error_reason', None))}"
    if detail:
        message += f"\n建议：{detail}"
    return message
