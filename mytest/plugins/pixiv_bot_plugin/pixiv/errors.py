"""Pixiv 请求失败的原因。不依赖 NoneBot。"""

from __future__ import annotations

from ..._shared.services.http import ServiceError

# 原因 → 给用户看的说明
REASONS = {
    "missing_cookie": "还没有设置 Pixiv Cookie，请管理员在 /webui「Pixiv」里填写。",
    "cookie_expired": "Pixiv 拒绝了当前 Cookie，可能已经过期，请管理员在 /webui「Pixiv」里换一个。",
    "not_found": "Pixiv 上没有这个作品或作者（可能已删除或设为不公开）。",
    "rate_limited": "Pixiv 返回了访问频率限制，请过几分钟再试。",
    "pixiv_server_error": "Pixiv 服务端暂时异常，请稍后再试。",
    "network_error": "连接 Pixiv 失败，可能是服务器网络、代理或 DNS 问题。",
    "bad_response": "Pixiv 返回的内容看不懂，可能接口改了，请看后台日志。",
    "not_started": "Pixiv 客户端没有启动，请重启机器人后再试。",
    "empty_keyword": "搜索关键词为空，请补充标签。",
    "no_qualified_images": "找到了搜索结果，但没有图片通过当前筛选条件。",
    "no_recent_images": "找到了标签，但指定时间范围内没有新图。",
    "no_results": "Pixiv 上这个标签没有作品。Pixiv 的标签多是日文，可以换成日文或英文试试（例：風景、オリジナル）。",
    "all_seen": "这个标签下能找到的好图最近都发过了，过几天再来，或者换个标签。",
    "low_bookmarks": "找到的作品收藏数都不够，可以把收藏筛选调低一点。",
    "all_blocked": "找到的作品都被屏蔽标签挡掉了（p站 偏好 查看）。",
}


def describe(reason: str | None) -> str:
    if not reason:
        return "没有找到符合条件的图片。"
    if reason in REASONS:
        return REASONS[reason]
    if reason.startswith("http_"):
        return f"Pixiv 返回 HTTP {reason.removeprefix('http_')}，请求没有成功。"
    return "请求过程中出现未知错误，请查看后台日志。"


class PixivError(ServiceError):
    """请求 Pixiv 失败。str(e) 是给用户看的说明，reason 给程序判断。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(describe(reason))
