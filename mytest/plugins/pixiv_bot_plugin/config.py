import os
from pathlib import Path

from nonebot import get_driver, logger

from .._shared.settings import SettingField, SettingSection, register

pixiv_settings = register(
    SettingSection(
        id="pixiv",
        title="Pixiv",
        description="搜图、每日一图、关注推送。喜好标签和屏蔽标签仍用 p站 偏好 指令管理。",
        order=50,
        fields=[
            SettingField(
                "cookie",
                "登录 Cookie",
                type="secret",
                env=("pixiv_cookie",),
                help="用来搜图、看作者作品、关注推送。浏览器登录 pixiv.net → 按 F12 → 「应用」（Application）→ Cookie → "
                "https://www.pixiv.net → 复制 PHPSESSID 的值（形如 12345678_AbCd…）粘贴到这里；整串 Cookie 也可以。"
                "保存后点右上角「测试连接」检查。建议用小号",
            ),
            SettingField(
                "image_proxy",
                "图片反代域名",
                default="i.isekai-inn.uno",
                help="替换 i.pximg.net；留空表示直连（需要服务器能访问 Pixiv）",
                env=("pixiv_bot_image_proxy",),
            ),
            SettingField(
                "max_page",
                "随机翻页的范围",
                type="int",
                default=100,
                minimum=1,
                maximum=1000,
                group="搜图",
                help="随机模式从前多少页里随机挑（每页 60 张，按发布时间从新到旧）。越大越不重复，但越往后作品越旧。"
                "标签作品不够这么多页时按实际页数算",
            ),
            SettingField(
                "premium",
                "Cookie 是 Pixiv 会员账号",
                type="bool",
                default=False,
                group="搜图",
                help="会员可以按热门排序搜，随机和热门模式会更容易搜到好图；不是会员时打开也没用（Pixiv 会忽略）",
            ),
            SettingField(
                "push_enabled",
                "推送关注作者的新作品",
                type="bool",
                default=False,
                group="关注推送",
                help="定时检查「p站 关注」列表里的作者，有新作品就发到下面的群。第一次检查只记下当前最新作品，不推送旧作",
            ),
            SettingField(
                "push_groups",
                "推送到这些群",
                type="lines",
                default=[],
                group="关注推送",
                help="每行一个群号。留空时不推送",
            ),
            SettingField(
                "push_interval",
                "检查间隔（分钟）",
                type="int",
                default=60,
                minimum=15,
                maximum=1440,
                group="关注推送",
                help="每个作者每轮请求一次 Pixiv，关注的人多时不要设得太短",
            ),
            SettingField(
                "push_max_per_author",
                "每个作者每轮最多推送几张",
                type="int",
                default=3,
                minimum=1,
                maximum=10,
                group="关注推送",
                help="超出的作品只提示数量，不发图",
            ),
            SettingField(
                "push_allow_r18",
                "推送 R-18 作品",
                type="bool",
                default=False,
                group="关注推送",
                help="关闭时跳过 R-18 / R-18G 作品；屏蔽标签（p站 偏好 屏蔽）始终生效",
            ),
        ],
    )
)


class _Config:
    data_dir = (
        str(getattr(get_driver().config, "pixiv_bot_data_dir", "") or "")
        or os.environ.get("PIXIV_BOT_DATA_DIR")
        or None
    )

    @property
    def image_proxy_host(self) -> str:
        return str(pixiv_settings.image_proxy).strip()


config = _Config()


def _resolve_data_dir() -> Path:
    if config.data_dir:
        return Path(config.data_dir)
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "pixiv_bot_plugin"
        logger.warning(f"[Pixiv] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"[Pixiv] 数据目录: {DATA_DIR.resolve()}")


def get_data_path(filename: str) -> Path:
    return DATA_DIR / filename
