from nonebot.adapters import Event

from .constants import PIXIV_PREFIX


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
    reason = getattr(spider, "last_error_reason", None)
    reason_text = {
        "missing_cookie": "还没有配置 Pixiv Cookie，请先使用 /p站 登录 [Cookie]。",
        "empty_cookie": "已保存的 Pixiv Cookie 是空的，请重新使用 /p站 登录 [Cookie]。",
        "cookie_load_error": "读取 Pixiv Cookie 失败，请检查本地数据文件权限，或重新登录。",
        "cookie_expired": "Pixiv 拒绝了当前 Cookie，可能已经过期，请重新登录。",
        "rate_limited": "Pixiv 返回了访问频率限制，请稍后再试。",
        "pixiv_server_error": "Pixiv 服务端暂时异常，请稍后再试。",
        "network_error": "连接 Pixiv 失败，可能是服务器网络、代理或 DNS 问题。",
        "session_not_initialized": "Pixiv 请求会话尚未初始化，请重启机器人后再试。",
        "empty_keyword": "搜索关键词为空，请补充标签。",
        "search_failed": "Pixiv 搜索接口没有返回可用结果，可能是 Cookie 失效或网络波动。",
        "no_qualified_images": "找到了搜索结果，但没有图片通过当前筛选条件。",
        "no_recent_images": "找到了标签，但指定时间范围内没有新图。",
        "unknown_error": "请求过程中出现未知错误，请查看后台日志。",
    }.get(reason)

    if reason_text is None and isinstance(reason, str) and reason.startswith("http_"):
        reason_text = f"Pixiv 返回 HTTP {reason.removeprefix('http_')}，请求没有成功。"

    if reason_text is None:
        reason_text = "没有找到符合条件的图片。"

    message = f"{action}「{target}」没有返回图片。\n原因：{reason_text}"
    if detail:
        message += f"\n建议：{detail}"
    return message
