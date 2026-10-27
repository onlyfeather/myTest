"""凛音的说说：写她自己 QQ 空间里的一条「营业」动态，配一张自拍。

只负责写文案，发送由 MaiBot 的凛音工具插件通过 Maizone 完成。
说说写的是凛音自己的生活和工作，不碰群聊内容；文风按真人随手发的样子要求，
写出来后再检查一遍常见的 AI 腔，不合格就让模型重写。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nonebot import logger

from .._shared.services import llm
from .._shared.services.http import ServiceError
from .._shared.settings import SettingField, SettingSection, register
from .._shared.settings import settings_store as _store

BEIJING = timezone(timedelta(hours=8))
PERSONA_FILE = Path(__file__).parents[1] / "dice_girl_plugin" / "personas.json"
MAX_ATTEMPTS = 3
HISTORY_SIZE = 8

DEFAULT_TOPICS = [
    "打扫书房", "擦银器", "熨衬衫", "试新到的茶叶", "窗外下雨", "床单晾不干", "去市场买菜",
    "自助结账机", "缎带系歪了", "修剪院子里的花", "烤司康", "换季整理衣柜", "夜里看书",
    "被热水烫了一下手", "新鞋磨脚", "停电", "早上起晚了五分钟", "新围裙", "路边的猫",
    "天突然冷了", "写购物清单", "洗了一下午碗", "睡不着", "下班把头发放下来",
]

qzone_settings = register(
    SettingSection(
        id="qzone",
        title="凛音的说说",
        description=(
            "凛音在 QQ 空间发的说说：写她自己的日常和工作，不提群聊和群友。"
            "主人让她发、或者 MaiBot 插件定时发时，由这里写文案、配一张自拍；发送走 Maizone 插件。"
        ),
        order=36,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField("model", "写文案的模型", default="",
                         help="留空用「大模型」里的模型。建议换成文笔好、不带思考的模型"),
            SettingField("topics", "题材", type="lines", default=DEFAULT_TOPICS,
                         help="每次随机挑几个给她参考（她也可以写别的），一行一个"),
            SettingField("extra", "额外要求", type="textarea", default="",
                         help="追加在写作要求最后，例如「最近是冬天，多写冷」"),
        ],
    )
)


def qzone_ready() -> str:
    """能写说说返回空字符串，否则返回原因。"""
    if not qzone_settings.enabled:
        return "发说说功能已关闭"
    if not llm.has_api_key():
        return "还没有配置大模型的 API Key"
    return ""


# ---------------------------------------------------------------- 提示词

STYLE = """文风是最要紧的：
- 像真人随手发的。1 到 3 句，一般 15 到 60 个字，每次长短不一样。可以只有半句，可以没有结尾。
- 写一个具体的小细节：某样东西、某个声音、一个数字、一个动作。不要概括感受，不要直接写“开心”“满足”“治愈”，让细节自己说。
- 不总结，不升华，不讲道理，不鼓励别人。不写“今天也要加油”“生活就是……”“愿……”“值得”“温柔”“美好”“小确幸”“岁月静好”。
- 不用排比和对仗，不用“不是……而是……”，不用“仿佛”“宛如”“犹如”。
- 不对读者说话：不写“大家”“你们”“各位”，结尾不提问。
- 标点照常用。感叹号最多一个，最好没有；不用波浪号、颜文字、话题标签；emoji 最多一个，通常不用。
- 不提 AI、机器人、设定、QQ、群、聊天。"""

EXAMPLES = """红茶罐见底了。明明上周才买的。
熨了一下午衬衫，袖口那颗扣子到现在还是歪的，明天再说。
下雨，床单晾不了，只好把客厅的窗户全擦了一遍。
自助结账机一直说“请将商品放入袋中”。我放了，它不信。
今日的司康烤得有些过头，不过主人应该吃不出来。
下班。头发终于可以放下来了"""

OUTPUT = (
    '只输出一个 JSON 对象，不要别的内容：\n'
    '{"text": "说说正文", "mood": "她此刻的心情，两到六个字", '
    '"scene": "配图自拍的情景：在哪、在做什么、穿什么、表情和姿势，要和正文、心情对得上"}'
)

WEEKDAYS = "一二三四五六日"


def _season(month: int) -> str:
    return {12: "冬天", 1: "冬天", 2: "冬天", 3: "春天", 4: "春天", 5: "春天",
            6: "夏天", 7: "夏天", 8: "夏天"}.get(month, "秋天")


def _load_role() -> dict[str, Any]:
    try:
        data = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
        roles = data.get("roles") or {}
        return roles.get(data.get("default_role") or "rinne") or next(iter(roles.values()), {})
    except (OSError, ValueError, AttributeError) as e:
        logger.warning("[说说] 读取人设失败，只用内置描述：{}", e)
        return {}


def _off_duty(role: dict[str, Any], hour: int) -> bool:
    start, end = (role.get("duty") or {}).get("night_hours") or [21, 6]
    return hour >= start or hour < end if start > end else start <= hour < end


def build_system_prompt(role: dict[str, Any], now: datetime, history: list[str]) -> str:
    persona = role.get("persona") or {}
    duty = role.get("duty") or {}
    off = _off_duty(role, now.hour)
    state = duty.get("off" if off else "on") or {}
    habits = "\n".join(f"- {h}" for h in persona.get("behavior_habits") or [])
    tone = (
        "现在是下班后：不用敬语，干脆一点，可以小小抱怨或嘴硬。但这里谁都能看到，不撒娇，不冲着人毒舌。"
        if off else
        "现在是工作时间：口吻得体、克制，冷幽默藏在礼貌里，可以用一点敬语，但别像公文。"
    )
    parts = [
        "你在替白鸟凛音写她自己 QQ 空间里的一条说说。",
        f"关于她：\n{persona.get('description') or '白鸟家的大小姐，自己上门当了女仆。'}",
        f"她的习惯：\n{habits}" if habits else "",
        f"当前状态：{state.get('label') or ('下班后' if off else '工作中')}。{tone}",
        "这个空间是她公开经营的“营业空间”，像偶像、主播维持人设那样，写她自己的日常和工作："
        "家务、红茶、天气、季节、出门买东西、看书、下班后的小情绪。可以偶尔带一句“主人”，"
        "但不写具体是谁，不写群里、聊天里发生的事，不点名任何人。",
        STYLE,
        f"参考这种感觉（不要照抄，也不要每次都写红茶）：\n{EXAMPLES}",
    ]
    if history:
        parts.append("最近发过的（题材和句式都不要重复）：\n" + "\n".join(history))
    if extra := str(qzone_settings.extra).strip():
        parts.append(f"另外：{extra}")
    parts.append(OUTPUT)
    return "\n\n".join(p for p in parts if p)


def build_user_prompt(now: datetime, topics: list[str], topic: str, mood: str) -> str:
    lines = [f"现在是{now.month}月{now.day}日，星期{WEEKDAYS[now.weekday()]}，{now.hour}点，{_season(now.month)}。"]
    if topic:
        lines.append(f"主人让她写这个：{topic}")
    elif topics:
        lines.append(f"可以从这里挑一个，也可以写别的：{'、'.join(topics)}")
    if mood:
        lines.append(f"她现在的心情：{mood}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 检查

_BANNED = (
    "加油", "生活就是", "岁月静好", "小确幸", "治愈", "温柔", "美好", "值得", "愿你", "愿我们",
    "仿佛", "宛如", "犹如", "大家", "你们", "各位", "宝子", "家人们", "AI", "人工智能", "机器人",
    "群里", "群友", "聊天", "～", "~", "#",
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_NOT_BUT = re.compile(r"不是.{1,15}而是")
MAX_LENGTH = 140


def ai_flavor_problems(text: str) -> list[str]:
    """找出文案里的 AI 腔，返回要改的地方；没问题返回空列表。"""
    problems = [f"用了“{w}”" for w in _BANNED if w in text]
    if _NOT_BUT.search(text):
        problems.append("用了“不是……而是……”")
    if text.count("！") + text.count("!") > 1:
        problems.append("感叹号太多")
    if len(_EMOJI.findall(text)) > 1:
        problems.append("emoji 太多")
    if len(text) > MAX_LENGTH:
        problems.append(f"太长了（{len(text)} 字），要短很多")
    if text.rstrip().endswith(("？", "?")):
        problems.append("结尾在提问")
    return problems


@dataclass
class Post:
    text: str
    mood: str
    scene: str

    @property
    def selfie_scene(self) -> str:
        return f"{self.scene}。表情和气氛：{self.mood}" if self.mood else self.scene


def parse_post(reply: str) -> Post:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ServiceError("写说说失败：大模型没有按格式返回")
    try:
        data = json.loads(reply[start:end + 1])
    except ValueError:
        raise ServiceError("写说说失败：大模型返回的 JSON 解析不了") from None
    if not isinstance(data, dict):
        raise ServiceError("写说说失败：大模型没有按格式返回")
    text = str(data.get("text") or "").strip().strip("“”\"「」").strip()
    scene = str(data.get("scene") or "").strip()
    if not text or not scene:
        raise ServiceError("写说说失败：大模型没有给出正文或配图情景")
    return Post(text=text, mood=str(data.get("mood") or "").strip()[:20], scene=scene)


# ---------------------------------------------------------------- 历史

def history_path() -> Path:
    return _store.path.parent / "qzone_history.json"


def load_history() -> list[dict[str, str]]:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logger.warning("[说说] 读取历史失败：{}", e)
        return []
    return [item for item in data if isinstance(item, dict) and item.get("text")] if isinstance(data, list) else []


def remember(text: str, now: datetime | None = None) -> None:
    now = now or datetime.now(BEIJING)
    items = [*load_history(), {"time": now.isoformat(timespec="minutes"), "text": text}][-HISTORY_SIZE:]
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 生成

async def write_post(*, topic: str = "", mood: str = "", now: datetime | None = None,
                     rng: random.Random | None = None) -> Post:
    """写一条说说。连续几次都有 AI 腔时抛 ServiceError，不发将就的文案。"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    rng = rng or random.Random()
    pool = [t for t in qzone_settings.topics if t]
    topics = rng.sample(pool, k=min(3, len(pool)))
    history = [item["text"] for item in load_history()[-5:]]
    messages = [
        {"role": "system", "content": build_system_prompt(_load_role(), now, history)},
        {"role": "user", "content": build_user_prompt(now, topics, topic, mood)},
    ]
    model = str(qzone_settings.model).strip()
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = await llm.chat(messages, temperature=0.9, model=model)
        post = parse_post(reply)
        problems = ai_flavor_problems(post.text)
        if not problems:
            logger.info("[说说] 第 {} 次写成：{}", attempt, post.text)
            return post
        logger.info("[说说] 第 {} 次有 AI 腔（{}）：{}", attempt, "、".join(problems), post.text)
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"这条不行：{'；'.join(problems)}。按要求重写，换个写法，还是只输出 JSON。"},
        ]
    raise ServiceError(f"写说说失败：连续 {MAX_ATTEMPTS} 次都有 AI 腔（{'、'.join(problems)}）")
