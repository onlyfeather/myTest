"""CoC 7 版的属性和技能表（基础值），以及技能名的统一写法。不依赖 NoneBot。

名字和骰娘的 .st 一致（侦查、理智、智力……），角色卡、模组、裁决结果都先过 canonical()。
"""

from __future__ import annotations

ATTRIBUTES = ("力量", "体质", "体型", "敏捷", "外貌", "智力", "意志", "教育", "幸运", "理智")

# 基础值；None 表示由属性推出（见 base_value）
SKILLS: dict[str, int | None] = {
    "会计": 5, "人类学": 1, "估价": 5, "考古学": 1, "魅惑": 15, "攀爬": 20,
    "计算机使用": 5, "信用评级": 0, "克苏鲁神话": 0, "乔装": 5, "闪避": None,
    "汽车驾驶": 20, "电气维修": 10, "电子学": 1, "话术": 5, "格斗": 25, "射击": 20,
    "急救": 30, "历史": 5, "恐吓": 15, "跳跃": 20, "母语": None, "外语": 1, "法律": 5,
    "图书馆使用": 20, "聆听": 20, "锁匠": 1, "机械维修": 10, "医学": 1, "博物学": 10,
    "领航": 10, "神秘学": 5, "操作重型机械": 1, "说服": 10, "驾驶": 1, "精神分析": 1,
    "心理学": 10, "骑术": 5, "科学": 1, "妙手": 10, "侦查": 25, "潜行": 20, "生存": 10,
    "游泳": 20, "投掷": 20, "追踪": 10, "艺术与手艺": 5, "动物驯养": 5, "潜水": 1,
    "爆破": 1, "读唇": 1, "催眠": 1, "炮术": 1,
}

# 骰娘的别名之外，跑团里常见的其他写法
ALIASES = {
    "图书馆": "图书馆使用", "计算机": "计算机使用", "电脑使用": "计算机使用",
    "驾驶汽车": "汽车驾驶", "开车": "汽车驾驶", "斗殴": "格斗", "手枪": "射击",
    "快速交谈": "话术", "信誉": "信用评级", "信用": "信用评级", "藏匿": "潜行",
    "开锁": "锁匠", "手艺": "艺术与手艺", "艺术": "艺术与手艺",
}

SOCIAL_SKILLS = ("魅惑", "话术", "恐吓", "说服", "心理学")


def canonical(name: str) -> str:
    from ...dice_girl_plugin.dice.coc import skill_name

    name = skill_name(name)
    return ALIASES.get(name, name)


def is_known(name: str) -> bool:
    name = canonical(name)
    return name in SKILLS or name in ATTRIBUTES


def base_value(name: str, sheet: dict[str, int]) -> int:
    """角色卡上没写这项时的默认值。"""
    name = canonical(name)
    if name == "闪避":
        return sheet.get("敏捷", 0) // 2
    if name == "母语":
        return sheet.get("教育", 0)
    return SKILLS.get(name) or 0
