"""
@Author         : yanyongyu
@Date           : 2022-10-15 08:08:41
@LastEditors    : yanyongyu
@LastEditTime   : 2023-03-30 18:24:49
@Description    : Template rendering helpers
@GitHub         : https://github.com/yanyongyu
"""

__author__ = "yanyongyu"


from datetime import datetime, timedelta

import humanize


def relative_time(time: datetime) -> timedelta:
    return datetime.now().astimezone() - time.astimezone()


def humanize_date(time: datetime) -> str:
    return humanize.naturaldate(time.astimezone())


def humanize_delta(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts[:2])


def humanize_size(size: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def usage_level(percent: int | float) -> str:
    if percent < 50:
        return "轻松"
    if percent < 75:
        return "正常"
    if percent < 90:
        return "偏忙"
    return "吃紧"
