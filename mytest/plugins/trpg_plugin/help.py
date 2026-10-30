from .._shared.help import HelpTopic, help_registry
from .config import trpg_groups

DETAIL = """\
只在跑团群里能用（WebUI「跑团」里配置）。

/开团 模组名 - 开一个团，进入招募
/加入 - 用自己在这个群的 .st 角色卡加入
/加入 角色名 - 用模组的预设角色加入
/开始 - 人齐了开场（开团的人）
/暂停、/继续、/结束 - 开团的人、群管理员
/离开 - 退出这个团，开场后下一轮离开
/kp 状态 - 当前场景、谁在团里

开团之后中途加入的人，下一轮登场。"""

help_registry.register(
    HelpTopic(
        key="kp",
        title="凛音当 KP",
        aliases=("开团", "团", "coc团"),
        summary=("/开团 模组名 / 加入 / 开始 - 凛音当 KP 跑团（开发中）",),
        detail=DETAIL,
        order=11,
        available=lambda: bool(trpg_groups()),
    )
)
