from .._shared.help import HelpTopic, help_registry
from .config import draw_settings

DETAIL = """\
/画图 描述 - 使用默认后端
/nai 描述 - NovelAI，擅长二次元插画；中文会自动翻译成 tag
/gpt画图 描述 - GPT Image，擅长写实、文字和复杂构图

尺寸：描述前加「横图」或「方图」，默认竖图
例：/nai 横图 海边的白发少女，夕阳
例：/gpt画图 方图 一张写着「欢迎光临」的咖啡店招牌

【参考图】
带着图片发指令，或者回复一张图再发指令，就会参考那张图
-参考 - 提示你再发一张参考图
NovelAI 用作角色参考：让画出来的角色长得像图里的人（只支持 V4.5 模型）
  加 -画风 连画风也一起参考
GPT Image 会综合参考图和描述来画
例：/nai -参考 坐在咖啡店里看书

【画师串】（只对 NovelAI 有效）
平时自动使用默认画师串，不会询问
-画师 - 询问新的画师串，记住后以后都用它
/nai画师 - 查看当前画师串
/nai画师 新画师串 - 直接修改
/nai画师 默认 - 恢复默认画师串
例：/nai -画师 -参考 海边散步

每人有冷却时间，失败不计冷却。"""

help_registry.register(
    HelpTopic(
        key="draw",
        title="画图",
        aliases=("ai画图", "绘画", "nai", "novelai", "gpt画图", "gpt", "参考图", "画师", "画师串", "nai画师"),
        summary=(
            "/画图 描述 - AI 画图（/nai、/gpt画图 指定后端）",
            "加 -参考 用参考图，加 -画师 换画师串",
        ),
        detail=DETAIL,
        admin=("管理员不受冷却限制；后端、模型、冷却等在 /webui 里设置",),
        order=20,
        available=lambda: bool(draw_settings.enabled),
    )
)
