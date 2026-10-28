"""凛音的说说：写她自己 QQ 空间里的一条「营业」动态，配一张自拍。

只负责写文案，发送由 MaiBot 的凛音工具插件通过 Maizone 完成。
说说写的是凛音自己的生活和工作，不碰群聊内容；文风按真人随手发的样子要求，
写出来后再检查一遍常见的 AI 腔，不合格就让模型重写。
"""

from __future__ import annotations

import calendar
import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
            "superuser 让她发、或者 MaiBot 插件定时发时，由这里写文案、配一张图（自拍或随手拍的东西）；"
            "发送走 Maizone 插件。"
            "定时说说分上午（工作中）、傍晚（刚下班）、深夜（睡前）三个时段，按时段拟一件事再写。"
            "「测试连接」会查一次天气。"
        ),
        order=36,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField("model", "写文案的模型", default="",
                         help="留空用「大模型」里的模型。建议换成文笔好、不带思考的模型"),
            SettingField("topics", "题材", type="lines", default=DEFAULT_TOPICS,
                         help="没指定题材时随机挑几个给她参考，定时说说也会用（她也可以写别的），一行一个"),
            SettingField("photo_ratio", "拍东西的比例（%）", type="int", default=35, minimum=0, maximum=100,
                         help="配图有多少比例不拍自己、改拍茶杯、窗外这些东西，其余是自拍。"
                         "拍东西用「AI 画图」的默认后端；画图关掉时全是自拍，没上传人设图时全拍东西"),
            SettingField("city", "城市", default="", placeholder="杭州",
                         help="写说说时查这个城市的实时天气给她参考（Open-Meteo，不用 API Key）；留空不查"),
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
- 像真人随手发的动态，写成一段话，不分行。长短按这次给的要求来，有时一句话，有时一大段。
- 从拟好的那件事说起，话多的时候可以顺带扯到旁边的小事、突然冒出来的念头，想到哪写到哪，不是每句都要有用，结尾可以没收住。
- 写具体的小细节：某样东西、某个声音、一个数字、一个动作。不要概括感受，不要直接写“开心”“满足”“治愈”，让细节自己说。
- 不总结，不升华，不讲道理，不鼓励别人。不写“今天也要加油”“生活就是……”“愿……”“值得”“温柔”“美好”“小确幸”“岁月静好”。
- 不用排比和对仗，不用“不是……而是……”，不用“仿佛”“宛如”“犹如”。
- 不对读者说话：不写“大家”“你们”“各位”，结尾不提问。
- 标点照常用。感叹号最多一个，最好没有；不用波浪号、颜文字、话题标签；emoji 最多一个，通常不用。
- 不提 AI、机器人、设定、QQ、群、聊天。"""


@dataclass(frozen=True)
class Length:
    """一条说说的长度档。hint 告诉模型这次写多长，low/high 是检查时放宽一点的范围。"""

    hint: str
    low: int
    high: int
    weight: int


# 真人发动态有时只有一个字，有时一大段；每次按权重随机抽一档
LENGTHS = (
    Length("这次只写一句话，10 到 40 字，甚至一两个字也行（比如只写“困。”）。不用铺垫，不用解释", 1, 50, 3),
    Length("这次写一段碎碎念，4 到 8 句、80 到 200 字", 60, 260, 6),
    Length("这次话多，写一大段，200 到 350 字，想到哪写到哪，可以从一件事扯到另一件事", 180, 420, 1),
)
NORMAL = LENGTHS[1]

# 示例池：长短和结构各不相同，每次抽两条，免得模型照着同一个句式写
EXAMPLES = (
    "困。",
    "银扣又掉了一颗。",
    "雨下到现在，床单晾了三天还是潮的。",
    "上周说磨脚的那双鞋，今天穿了一整天，没磨。看来是它先认输了。",
    "刚才把糖当成盐放进汤里，尝了一口，决定这锅汤从现在起叫甜汤。",
    "下班。头发放下来了，今天什么都不想",
    "上午把书房的窗户全擦了一遍，擦完才发现外面起雾了，等于白擦。银器也只擦了一半，剩下那半留给明天的我。"
    "红茶罐又见底了，明明上周才补过货。我怀疑宅子里有人半夜偷喝，可惜没有证据，只好先记在购物清单上。",
    "下班。发带解下来的那一下，头皮终于活过来了。晚饭是中午剩的司康，放了半天有点硬，泡进红茶里勉强能吃。"
    "下午去超市，自助结账机一直说“请将商品放入袋中”，我放了，它不信，僵持了三个来回，最后还是叫了店员。明天换一家买。",
    "睡不着。把明天的购物清单写了两遍，第二遍比第一遍多出三样东西，也不知道是从哪冒出来的。"
    "窗外有只猫在叫，叫了快半小时，听起来比我还清醒。衬衫还挂在阳台上没收，算了，明早再说",
    "今天的成绩：熨好衬衫四件，擦完银器一整套，打碎茶杯零个。最后一项是我自己加的，写上去心情好一点。"
    "新到的那罐茶也拆了，香味比罐子上写的淡，倒也不算骗人。",
    "楼下卖花的阿姨说今天的玫瑰不新鲜，劝我买雏菊。我买了雏菊。回来要插的时候才想起来，"
    "宅子里没有一个花瓶配得上雏菊，现在它插在一个果酱罐里，看着比我还自在。",
    "换季整理衣柜，整理到一半就坐在地上了。冬天的大衣还是去年那三件，袖口那件的扣子我说了一整年要缝，"
    "到今天也没缝。翻出一条完全不记得什么时候买的围巾，灰色的，标签都没拆，大概是某个冲动的下午。"
    "顺手试了一下，还挺暖，就是颜色像抹布，戴出去会被认成刚干完活。又翻出一封信，是离家那年写给自己的，"
    "没敢拆，原样塞回去了。最后衣柜比整理前还满，因为拿出来的东西一件都没舍得扔。晚饭随便煮了面，"
    "面汤里飘着一片葱，我盯着它看了很久，然后想起来红茶还泡在壶里，已经苦得不能喝了。",
)

SELFIE = "selfie"
PHOTO = "photo"
SCENE_FORMATS = {
    SELFIE: "配图自拍的情景：在哪、在做什么、穿什么、表情和姿势，要和正文、心情对得上",
    PHOTO: "配图是她随手拍的一样东西或一处景，画面里没有人：拍的是什么、在哪、光线和角度，要和正文对得上",
}
PHOTO_HINT = (
    "这条的配图不拍她自己，是她随手拍的东西：手边的茶杯、窗外、刚做好的点心、路边的猫之类，"
    "拍的东西要是正文里写到的。画面里不要出现人。"
)


def output_format(photo: str) -> str:
    return (
        '只输出一个 JSON 对象，不要别的内容：\n'
        '{"event": "先拟好的那件具体小事，一句话", "text": "说说正文，从这件事说开", '
        f'"mood": "她此刻的心情，两到六个字", "scene": "{SCENE_FORMATS[photo]}"}}'
    )


WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Slot:
    label: str
    off_duty: bool
    hint: str
    outfit: str  # 自拍时的穿着，拍东西时不用


# 定时说说的三个时段。MaiBot 插件到点后先判定发不发，通过了才按时段写
SLOTS = {
    "work": Slot(
        "上午，工作中", False,
        "她正在工作，抽空发一条。先给她拟一件上午干活时真会碰到的小事（手上的家务、准备的东西、出的小岔子），再照着写。",
        "配图是工作中的样子：女仆装，头发盘起，在宅子里干活的地方。",
    ),
    "off_work": Slot(
        "傍晚，刚下班", True,
        "她刚下班，正从工作状态里切出来：解发带、松袖扣、回房间、吃晚饭。先给她拟一件刚下班那会儿的小事，再照着写，"
        "语气比上班时松，可以有点累。",
        "配图是刚下班：女仆装还没换或刚换下，头发放下来扎成侧马尾。",
    ),
    "bedtime": Slot(
        "深夜，睡前", True,
        "她准备睡了。先给她拟一件睡前的小事或一个念头（明天的安排、没做完的活、窗外的声音、睡不着），再照着写，"
        "语气安静，可以困，想到哪说到哪。",
        "配图是睡前：睡衣，头发放下，房间里只开着小灯。",
    ),
}


def _season(month: int) -> str:
    return {12: "冬天", 1: "冬天", 2: "冬天", 3: "春天", 4: "春天", 5: "春天",
            6: "夏天", 7: "夏天", 8: "夏天"}.get(month, "秋天")


# 农历节日对应的公历日期：春节、端午、七夕、中秋。表外的年份只算公历节日
_LUNAR = {
    2026: ("02-17", "06-19", "08-19", "09-25"),
    2027: ("02-06", "06-09", "08-08", "09-15"),
    2028: ("01-26", "05-28", "08-26", "10-03"),
    2029: ("02-13", "06-16", "08-16", "09-22"),
    2030: ("02-03", "06-05", "08-05", "09-12"),
    2031: ("01-23", "06-24", "08-24", "10-01"),
    2032: ("02-11", "06-12", "08-12", "09-19"),
    2033: ("01-31", "06-01", "08-01", "09-08"),
    2034: ("02-19", "06-20", "08-20", "09-27"),
    2035: ("02-08", "06-10", "08-10", "09-16"),
}
_SOLAR = {
    "01-01": "元旦", "02-14": "情人节", "05-01": "劳动节", "10-01": "国庆节", "10-31": "万圣夜",
    "12-24": "平安夜", "12-25": "圣诞节", "12-31": "跨年夜",
    **{f"10-0{d}": "国庆假期" for d in range(2, 8)},
}


def holidays(year: int) -> dict[date, str]:
    days = {date.fromisoformat(f"{year}-{md}"): name for md, name in _SOLAR.items()}
    if lunar := _LUNAR.get(year):
        spring, dragon, qixi, mid = (date.fromisoformat(f"{year}-{md}") for md in lunar)
        days |= {spring + timedelta(days=d): "春节假期" for d in range(1, 7)}
        days |= {spring - timedelta(days=1): "除夕", spring: "春节", spring + timedelta(days=14): "元宵节",
                 dragon: "端午节", qixi: "七夕", mid: "中秋节"}
    return days


def day_notes(day: date) -> list[str]:
    """今天是不是周末、节日、月初月底，节日前三天也提一句。"""
    notes = ["周末"] if day.weekday() >= 5 else []
    table = holidays(day.year)
    if name := table.get(day):
        notes.append(f"今天是{name}")
    else:
        for ahead, word in ((1, "明天"), (2, "后天"), (3, "大后天")):
            if name := table.get(day + timedelta(days=ahead)):
                notes.append(f"{word}是{name}")
                break
    if day.day <= 3:
        notes.append("月初")
    elif day.day > calendar.monthrange(day.year, day.month)[1] - 3:
        notes.append("月底")
    return notes


def pick_length(rng: random.Random) -> Length:
    return rng.choices(LENGTHS, weights=[x.weight for x in LENGTHS])[0]


def pick_examples(length: Length, rng: random.Random) -> list[str]:
    """抽两条示例：一条长短合这次的档位，另一条随便抽，句式不一样。"""
    fits = [e for e in EXAMPLES if length.low <= len(e) <= length.high]
    first = rng.choice(fits or list(EXAMPLES))
    return [first, rng.choice([e for e in EXAMPLES if e != first])]


def _history_line(item: dict[str, str], now: datetime) -> str:
    try:
        then = datetime.fromisoformat(item.get("time") or "").astimezone(BEIJING)
    except ValueError:
        return f"- {item['text']}"
    days = (now.date() - then.date()).days
    ago = "今天" if days <= 0 else "昨天" if days == 1 else f"{days} 天前"
    return f"- {then.month}月{then.day}日（{ago}）：{item['text']}"


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


def build_system_prompt(role: dict[str, Any], now: datetime, history: list[str], slot: Slot | None = None,
                        examples: list[str] | None = None, photo: str = SELFIE) -> str:
    persona = role.get("persona") or {}
    duty = role.get("duty") or {}
    off = slot.off_duty if slot else _off_duty(role, now.hour)
    state = {"label": slot.label} if slot else duty.get("off" if off else "on") or {}
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
        "参考这种感觉（不要照抄，长短和句式也别跟着学，不要每次都写红茶）：\n"
        + "\n\n".join(examples if examples is not None else EXAMPLES[:2]),
    ]
    if history:
        parts.append(
            "最近发过的（题材和句式都不要重复）：\n" + "\n".join(history)
            + "\n偶尔可以接着之前说过的事往下讲（那件事后来怎么样了），但只是偶尔，大多数时候写新的事。",
        )
    if extra := str(qzone_settings.extra).strip():
        parts.append(f"另外：{extra}")
    parts.append(output_format(photo))
    return "\n\n".join(p for p in parts if p)


def build_user_prompt(now: datetime, topics: list[str], topic: str, mood: str, slot: Slot | None = None,
                      length: Length = NORMAL, *, weather: str = "", photo: str = SELFIE) -> str:
    when = f"{now.month}月{now.day}日，星期{WEEKDAYS[now.weekday()]}，{now.hour}点，{_season(now.month)}"
    if notes := day_notes(now.date()):
        when += f"，{'，'.join(notes)}"
    lines = [f"现在是{when}。"]
    if weather:
        lines.append(f"她那边的天气：{weather}。可以写进去，也可以不提，别写成天气预报。")
    if topic:
        lines.append(f"这次要写的题材：{topic}")
    else:
        if slot:
            lines.append(slot.hint + (slot.outfit if photo == SELFIE else ""))
        if topics:
            lead = "想不出来的话，可以从这里挑一个合这个时段的" if slot else "可以从这里挑一个"
            lines.append(f"{lead}，也可以写别的：{'、'.join(topics)}")
    if photo == PHOTO:
        lines.append(PHOTO_HINT)
    lines.append(length.hint + "。")
    if mood:
        lines.append(f"她现在的心情：{mood}")
    return "\n".join(lines)


async def test_weather() -> str:
    """「测试连接」按钮：按填的城市查一次天气。"""
    from .weather import fetch

    city = str(qzone_settings.city).strip()
    if not city:
        return "没有填城市，说说不带天气"
    return f"天气查到了：{await fetch(city)}"


# ---------------------------------------------------------------- 检查

_BANNED = (
    "加油", "生活就是", "岁月静好", "小确幸", "治愈", "温柔", "美好", "值得", "愿你", "愿我们",
    "仿佛", "宛如", "犹如", "大家", "你们", "各位", "宝子", "家人们", "AI", "人工智能", "机器人",
    "群里", "群友", "聊天", "～", "~", "#",
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_NOT_BUT = re.compile(r"不是.{1,15}而是")


def ai_flavor_problems(text: str, length: Length = NORMAL) -> list[str]:
    """找出文案里的 AI 腔和不合这次长度档的地方，返回要改的地方；没问题返回空列表。"""
    problems = [f"用了“{w}”" for w in _BANNED if w in text]
    if _NOT_BUT.search(text):
        problems.append("用了“不是……而是……”")
    if text.count("！") + text.count("!") > 1:
        problems.append("感叹号太多")
    if len(_EMOJI.findall(text)) > 1:
        problems.append("emoji 太多")
    if len(text) > length.high:
        problems.append(f"太长了（{len(text)} 字）。{length.hint}")
    if len(text) < length.low:
        problems.append(f"太短了（{len(text)} 字）。{length.hint}")
    if text.rstrip().endswith(("？", "?")):
        problems.append("结尾在提问")
    return problems


@dataclass
class Post:
    text: str
    mood: str
    scene: str
    event: str = ""
    photo: str = SELFIE  # 配图是自拍（SELFIE）还是拍东西（PHOTO）

    @property
    def selfie_scene(self) -> str:
        return f"{self.scene}。表情和气氛：{self.mood}" if self.mood else self.scene

    @property
    def photo_prompt(self) -> str:
        return f"随手用手机拍的照片，画面里没有人：{self.scene}。气氛：{self.mood}" if self.mood \
            else f"随手用手机拍的照片，画面里没有人：{self.scene}"


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
    return Post(text=text, mood=str(data.get("mood") or "").strip()[:20], scene=scene,
                event=str(data.get("event") or "").strip()[:100])


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

def pick_photo(rng: random.Random, *, can_selfie: bool, can_photo: bool) -> str:
    """这次配自拍还是拍东西。两样都不行时返回空字符串。"""
    if not (can_selfie and can_photo):
        return SELFIE if can_selfie else PHOTO if can_photo else ""
    return PHOTO if rng.random() * 100 < int(qzone_settings.photo_ratio) else SELFIE


async def write_post(*, topic: str = "", mood: str = "", slot: str = "", now: datetime | None = None,
                     rng: random.Random | None = None, photo: str = SELFIE) -> Post:
    """写一条说说。slot 是定时说说的时段（见 SLOTS），按时段先拟一件事再写；photo 是配图类型。
    每次随机抽一个长度档和两条示例；连续几次都有 AI 腔时抛 ServiceError，不发将就的文案。"""
    from .weather import current_weather

    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    rng = rng or random.Random()
    period = SLOTS.get(slot)
    pool = [t for t in qzone_settings.topics if t]
    topics = rng.sample(pool, k=min(3, len(pool)))
    length = pick_length(rng)
    examples = pick_examples(length, rng)
    history = [_history_line(item, now) for item in load_history()[-5:]]
    weather = await current_weather(str(qzone_settings.city))
    messages = [
        {"role": "system", "content": build_system_prompt(_load_role(), now, history, period, examples, photo)},
        {"role": "user", "content": build_user_prompt(now, topics, topic, mood, period, length,
                                                      weather=weather, photo=photo)},
    ]
    model = str(qzone_settings.model).strip()
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = await llm.chat(messages, temperature=0.9, model=model)
        post = parse_post(reply)
        post.photo = photo
        problems = ai_flavor_problems(post.text, length)
        if not problems:
            logger.info("[说说] 第 {} 次写成（{}）：{}", attempt, post.event or "没写事件", post.text)
            return post
        logger.info("[说说] 第 {} 次有 AI 腔（{}）：{}", attempt, "、".join(problems), post.text)
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"这条不行：{'；'.join(problems)}。按要求重写，换个写法，还是只输出 JSON。"},
        ]
    raise ServiceError(f"写说说失败：连续 {MAX_ATTEMPTS} 次都有 AI 腔（{'、'.join(problems)}）")
