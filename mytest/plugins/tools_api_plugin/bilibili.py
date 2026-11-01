"""凛音刷 B 站：按要求找视频发出来，看懂别人发的视频链接。

找视频：搜 B 站 → 发「封面 + 标题 + 链接」到聊天里，凛音自己接一句。
看视频：读标题、简介，有字幕时读字幕（要登录 Cookie），交给凛音当作「看过了」来聊；不往群里发东西。
抖音反爬太严，不做。
"""

from __future__ import annotations

import random
from typing import Any

from .._shared import proxy
from .._shared.services.bilibili import ORDERS, Bilibili, Video
from .._shared.settings import SettingField, SettingSection, proxy_for, register, settings_store

bilibili_settings = register(
    SettingSection(
        id="bilibili",
        title="B站",
        description="凛音找 B 站视频、看懂群友发的 B 站链接。不填 Cookie 也能搜视频、读简介；"
                    "读字幕要登录 Cookie。「测试连接」会搜一次视频。",
        order=55,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField("cookie", "登录 Cookie", type="secret", default="",
                         help="可选，填了能读视频字幕，搜索也不容易被拦。浏览器登录 bilibili.com → F12 →「应用」→ Cookie → "
                              "复制 SESSDATA 的值粘贴到这里（整串 Cookie 也行）。建议用小号，Cookie 大约一个月过期"),
            SettingField("read_subtitle", "看视频时读字幕", type="bool", default=True,
                         help="有字幕的视频，凛音能知道视频里说了什么；没有字幕时只看标题和简介。要填登录 Cookie"),
            SettingField("send_cover", "找视频时带封面", type="bool", default=True),
            proxy.mode_field(),
            proxy.custom_field(),
            SettingField("timeout", "超时（秒）", type="float", default=15.0, minimum=5, maximum=120),
        ],
    )
)


def client() -> Bilibili:
    s = bilibili_settings
    return Bilibili(cookie=str(s.cookie), proxy=proxy_for(s), timeout=float(s.timeout))


def ready() -> str:
    return "" if bilibili_settings.enabled else "B 站功能已关闭"


def pick(videos: list[Video], count: int, rng: random.Random | None = None) -> list[Video]:
    """从前 10 个里挑：第一个最相关的必选，其余随机，免得每次同样的词都发同一批。"""
    top = videos[:10]
    if len(top) <= count:
        return top
    rng = rng or random.Random()
    return [top[0], *rng.sample(top[1:], count - 1)]


def card_text(video: Video) -> str:
    meta = "，".join(x for x in (f"UP：{video.author}" if video.author else "",
                                 video.duration, f"{video.play} 播放" if video.play else "") if x)
    return f"{video.title}\n{meta}\n{video.url}" if meta else f"{video.title}\n{video.url}"


def describe_for_rinne(video: Video, subtitle: str) -> str:
    parts = [f"视频「{video.title}」", f"UP 主：{video.author}" if video.author else "",
             f"时长 {video.duration}" if video.duration else "", f"{video.play} 播放" if video.play else ""]
    lines = ["，".join(p for p in parts if p)]
    if video.desc:
        lines.append(f"简介：{video.desc[:300]}")
    lines.append(f"字幕（视频里说的话）：{subtitle}" if subtitle else "这个视频没有字幕，你只知道标题和简介。")
    return "\n".join(lines)


async def test_connection() -> str:
    api = client()
    videos = await api.search("猫")
    name = await api.login_name() if api.cookie else ""
    login = f"已登录：{name}" if name else ("Cookie 无效或已过期，读不了字幕" if api.cookie else "没有填 Cookie，读不了字幕")
    return f"搜索正常，找到 {len(videos)} 个视频。{login}"


settings_store.set_tester("bilibili", test_connection)


def order_of(mode: Any) -> str:
    return ORDERS.get(str(mode or "relevant"), ORDERS["relevant"])
