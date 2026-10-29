"""点歌的配置。音源地址和密钥放环境变量；开关、白名单放 WebUI。

环境变量：
    NETEASE_API_URL=http://netease-api:3000   # 可选，api-enhanced；不填就搜不到网易云
    KUGOU_API_URL=http://kugou-api:3000       # 可选，KuGouMusicApi；不填就搜不到酷狗
    MUSIC_CREDENTIAL_KEY=32 字节 base64       # 可选；bot 会员账号凭证的加密密钥。不填就不能扫码登录会员账号
    MUSIC_CREDENTIAL_KEY_OLD=                 # 换密钥时临时填旧密钥，凭证会用新密钥重新加密

以前叫 LISTEN_CREDENTIAL_KEY，旧名字仍然认，不用改 .env。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from .._shared.settings import SettingField, SettingSection, register

music_settings = register(
    SettingSection(
        id="music",
        title="点歌",
        description=(
            "群里发 /点歌 歌名，从 QQ 音乐、网易云、酷狗三个平台搜歌，回复编号发音乐卡片。"
            "网易云和酷狗需要在环境变量里配好 NETEASE_API_URL、KUGOU_API_URL。"
        ),
        order=60,
        fields=[
            SettingField("enabled", "启用点歌", type="bool", default=True),
            SettingField(
                "allowed_groups",
                "允许的群",
                type="lines",
                default=[],
                help="每行一个群号。留空表示所有群都能用",
            ),
            SettingField("allow_private", "私聊也能用", type="bool", default=True),
            SettingField(
                "per_platform",
                "每个平台列几首",
                type="int",
                default=3,
                minimum=1,
                maximum=5,
            ),
            SettingField(
                "cooldown",
                "每人冷却（秒）",
                type="int",
                default=5,
                minimum=0,
                maximum=600,
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


def credential_keys() -> tuple[str, str]:
    """(当前密钥, 旧密钥)。兼容一起听时期的 LISTEN_CREDENTIAL_KEY。"""
    current = _env("music_credential_key") or _env("listen_credential_key")
    old = _env("music_credential_key_old") or _env("listen_credential_key_old")
    return current, old


def api_url(name: str) -> str:
    value = _env(name).rstrip("/")
    return value if urlsplit(value).scheme in ("http", "https") else ""
