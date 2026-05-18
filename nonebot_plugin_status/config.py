"""
@Author         : yanyongyu
@Date           : 2020-10-04 16:32:00
@LastEditors    : yanyongyu
@LastEditTime   : 2023-03-30 18:17:09
@Description    : Config for status plugin
@GitHub         : https://github.com/yanyongyu
"""

__author__ = "yanyongyu"


from pydantic import BaseModel

CPU_TEMPLATE = r"CPU：{{ '%.0f' % cpu_usage }}%（{{ cpu_usage | usage_level }}）"
"""Default CPU status template."""

# PER_CPU_TEMPLATE = (
#     "CPU:\n"
#     "{%- for core in per_cpu_usage %}\n"
#     "  core{{ loop.index }}: {{ '%02d' % core }}%\n"
#     "{%- endfor %}"
# )

MEMORY_TEMPLATE = (
    r"内存：{{ '%.0f' % memory_usage.percent }}%（{{ memory_usage.percent | usage_level }}，"
    r"已用 {{ memory_usage.used | humanize_size }} / 共 {{ memory_usage.total | humanize_size }}）"
)
"""Default memory status template."""

SWAP_TEMPLATE = (
    r"{% if swap_usage.total %}交换分区：{{ '%.0f' % swap_usage.percent }}%（{{ swap_usage.percent | usage_level }}）{% endif +%}"
)
"""Default swap status template."""

DISK_TEMPLATE = (
    "磁盘：\n"
    "{% for name, usage in disk_usage.items() %}\n"
    "  {{ name }}：{{ '%.0f' % usage.percent }}%（{{ usage.percent | usage_level }}，剩余 {{ usage.free | humanize_size }}）\n"
    "{% endfor %}"
)
"""Default disk status template."""

UPTIME_TEMPLATE = "系统已运行：{{ uptime | relative_time | humanize_delta }}"
"""Default uptime status template."""

RUNTIME_TEMPLATE = "机器人已在线：{{ runtime | relative_time | humanize_delta }}"
"""Default runtime status template."""


class Config(BaseModel):
    server_status_enabled: bool = True
    """Whether to enable the server status commands."""
    server_status_truncate: bool = True
    """Whether to render the status template with used variables only."""
    server_status_only_superusers: bool = True
    """Whether to allow only superusers to use the status commands."""

    server_status_template: str = "\n".join(
        (
            "服务器状态",
            "状态：在线，可接收消息",
            CPU_TEMPLATE,
            MEMORY_TEMPLATE,
            RUNTIME_TEMPLATE,
            SWAP_TEMPLATE,
            DISK_TEMPLATE,
        )
    )
    """Default server status template.

    Including:

    - CPU usage
    - Memory usage
    - Runtime
    - Swap usage
    - Disk usage
    """
