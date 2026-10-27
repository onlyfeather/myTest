from .._shared.help import HelpTopic, help_registry
from .config import draw_settings

DETAIL = """\
/画图 描述 - 使用默认后端
/nai 描述 - NovelAI，擅长二次元插画；中文会自动翻译成 tag
/gpt画图 描述 - GPT Image，擅长写实、文字和复杂构图

尺寸：描述前加「横图」或「方图」，默认竖图
例：/nai 横图 海边的白发少女，夕阳
例：/gpt画图 方图 一张写着「欢迎光临」的咖啡店招牌

每人有冷却时间，失败不计冷却。"""

help_registry.register(
    HelpTopic(
        key="draw",
        title="画图",
        aliases=("ai画图", "绘画", "nai", "novelai", "gpt画图", "gpt"),
        summary=("/画图 描述 - AI 画图（/nai、/gpt画图 指定后端）",),
        detail=DETAIL,
        admin=("管理员不受冷却限制；后端、模型、冷却等在 /webui 里设置",),
        order=20,
        available=lambda: bool(draw_settings.enabled),
    )
)
