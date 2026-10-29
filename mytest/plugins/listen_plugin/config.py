"""一起听的配置。部署相关和密钥放环境变量；白名单群等日常调整的放 WebUI。

环境变量：
    LISTEN_PUBLIC_URL=https://listen.example.com   # 必填，生成登录链接、检查 Origin 都用它
    LISTEN_SECRET=至少 32 位随机字符串               # 必填，不设置就不启动
    LISTEN_HOST=127.0.0.1                          # Docker 里改成 0.0.0.0，并且只映射到宿主机本地
    LISTEN_PORT=8090
    LISTEN_TRUSTED_PROXIES=127.0.0.1,::1           # 只采信这些地址传来的 X-Forwarded-For
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .._shared.settings import SettingField, SettingSection, register

listen_settings = register(
    SettingSection(
        id="listen",
        title="一起听",
        description=(
            "白名单里的群可以在群里发 /一起听 开房间，只有这些群的成员能进。"
            "端口、域名、密钥在环境变量里设置（LISTEN_PUBLIC_URL、LISTEN_SECRET）。"
        ),
        order=60,
        fields=[
            SettingField(
                "allowed_groups",
                "白名单群",
                type="lines",
                default=[],
                help="每行一个群号。留空表示不开放",
                env=("listen_allowed_groups",),
            ),
            SettingField(
                "max_members",
                "每个房间最多几人",
                type="int",
                default=10,
                minimum=1,
                maximum=50,
            ),
            SettingField(
                "bitrate",
                "音质",
                type="select",
                default="320",
                options=(("320", "320kbps"), ("128", "128kbps")),
                help="拿不到 320kbps 时自动用 128kbps",
            ),
            SettingField(
                "downgrade_over",
                "超过几人时降到 128kbps",
                type="int",
                default=10,
                minimum=1,
                maximum=50,
                help="服务器带宽 5Mbps 时，320kbps 大约能撑 12 人",
            ),
        ],
    )
)


def _env(name: str) -> str:
    try:
        from nonebot import get_driver

        value = getattr(get_driver().config, name.lower(), None)
        if value not in (None, ""):
            return str(value).strip()
    except ValueError:
        pass
    return os.environ.get(name.upper(), "").strip()


@dataclass(frozen=True)
class Deployment:
    host: str
    port: int
    public_url: str
    secret: str
    trusted_proxies: tuple[str, ...]


def load_deployment() -> tuple[Deployment | None, str]:
    """返回 (配置, 不能启动的原因)。"""
    secret = _env("listen_secret")
    public_url = _env("listen_public_url").rstrip("/")
    if not secret:
        return None, "没有设置 LISTEN_SECRET"
    if len(secret) < 32:
        return None, f"LISTEN_SECRET 只有 {len(secret)} 位，至少需要 32 位"
    parts = urlsplit(public_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, "LISTEN_PUBLIC_URL 没有设置或格式不对，例如 https://listen.example.com"
    try:
        port = int(_env("listen_port") or 8090)
    except ValueError:
        return None, "LISTEN_PORT 不是数字"
    proxies = tuple(p.strip() for p in (_env("listen_trusted_proxies") or "127.0.0.1,::1").split(",") if p.strip())
    return Deployment(
        host=_env("listen_host") or "127.0.0.1",
        port=port,
        public_url=f"{parts.scheme}://{parts.netloc}",
        secret=secret,
        trusted_proxies=proxies,
    ), ""
