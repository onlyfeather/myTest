from .._shared.help import HelpTopic, help_registry

DETAIL = """\
【投骰】
.rd - 随机 1-100
.rd 20 - 随机 1-20
.rd 100 60 侦查 - 带目标值的检定，凛音会点评结果
/dice 投骰 … - 和 .rd 一样

【状态】
/dice 状态 - 查看凛音对你的好感度

【角色】
/dice 角色列表 - 查看可用的点评角色
/dice 切换角色 角色ID - 群里需要群主或管理员

好感度只影响点评的称呼和语气，不影响骰点结果。"""

help_registry.register(
    HelpTopic(
        key="dice",
        title="投骰",
        aliases=("骰娘", "骰子", "dice", "rd", "trpg", "跑团"),
        summary=(".rd [面数] [目标值] [事件] - 投骰并由凛音点评", "/dice 状态 - 查看好感度"),
        detail=DETAIL,
        admin=(
            "以下仅限管理员私聊：",
            "/dice AI 开启|关闭|状态 - 投骰点评的 AI 开关",
            "/dice 设置好感 QQ号 数值 [角色ID]",
        ),
        order=10,
    )
)
