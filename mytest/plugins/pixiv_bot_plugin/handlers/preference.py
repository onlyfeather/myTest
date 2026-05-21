from nonebot_plugin_alconna import Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import pixiv_command
from ..pixiv import get_pixiv_spider
from ..services.preference import handle_alias, handle_preference


content_preference = Alconna(
    pixiv_command("偏好"),
    Args["action", str]["tag", str, None],
)

alias_manage = Alconna(
    pixiv_command("圈名"),
    Args["action", str]["user_id", str]["alias", str, None],
)

preference_matcher = on_alconna(content_preference, use_cmd_start=True, block=True)
alias_matcher = on_alconna(alias_manage, use_cmd_start=True, block=True)


@preference_matcher.handle()
async def content_preference_handle(result: Arparma):
    action = result.query[str]("action")
    tag = result.query[str]("tag", None)

    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(handle_preference(spider, action, tag)).send()

    except Exception as e:
        await UniMessage.text(f"内容偏好管理时发生错误：{str(e)}").send()


@alias_matcher.handle()
async def alias_manage_handle(result: Arparma):
    action = result.query[str]("action")
    user_id = result.query[str]("user_id")
    alias = result.query[str]("alias", None)

    try:
        async with get_pixiv_spider() as spider:
            await UniMessage.text(handle_alias(spider, action, user_id, alias)).send()

    except Exception as e:
        await UniMessage.text(f"圈名管理时发生错误：{str(e)}").send()


__all__ = [
    "alias_matcher",
    "preference_matcher",
]
