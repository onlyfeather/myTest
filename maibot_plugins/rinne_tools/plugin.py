"""凛音的工具：把 NoneBot 的功能交给凛音在聊天中使用。

工具本身不在这里实现，而是调用同一台服务器上 NoneBot 的 /tools 接口。
NoneBot 负责执行、限流，并把结果（图片、语音）直接发到聊天里；
这里只把「谁、在哪、要做什么」传过去，再把给凛音看的说明返回给 MaiBot。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

BEIJING = timezone(timedelta(hours=8))
MAIZONE_API = "internetsb.maizone.send_feed_api"

# ---------------------------------------------------------------- 配置


class GeneralSection(PluginConfigBase):
    __ui_label__ = "连接 NoneBot"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本（自动维护，请勿修改）")
    enabled: bool = Field(default=True, description="是否启用凛音的工具")
    nonebot_url: str = Field(
        default="http://127.0.0.1:8080",
        description="NoneBot 的地址（只用本机或内网地址）",
        json_schema_extra={"placeholder": "http://127.0.0.1:8080"},
    )
    token: str = Field(
        default="",
        description="和 NoneBot 的 TOOLS_API_TOKEN 填同一个值",
        json_schema_extra={"input_type": "password"},
    )
    timeout: float = Field(default=20.0, description="请求超时（秒）。工具只负责开始任务，很快就会返回")


class ToolsSection(PluginConfigBase):
    __ui_label__ = "开放哪些工具"
    __ui_order__ = 1

    draw: bool = Field(default=True, description="画图")
    selfie: bool = Field(default=True, description="自拍")
    tts: bool = Field(default=True, description="语音")
    dice: bool = Field(default=True, description="投骰")
    pixiv: bool = Field(default=True, description="Pixiv 搜图和每日推荐")
    qzone: bool = Field(default=True, description="发说说（只有主人能让她发，需要装 Maizone 插件）")


class QzoneSection(PluginConfigBase):
    __ui_label__ = "说说"
    __ui_order__ = 2

    auto_enabled: bool = Field(default=False, description="定时自己发说说（开之前先把 Maizone 的定时发送关掉）")
    superuser_id: str = Field(default="", description="NoneBot 的 superuser QQ 号。定时说说用这个身份请求，NoneBot 会核对")
    work_time: str = Field(default="10:00", description="上午、工作中的时段（北京时间 HH:MM，留空不发）")
    off_work_time: str = Field(default="18:00", description="傍晚、刚下班的时段（留空不发）")
    bedtime_time: str = Field(default="23:00", description="深夜、睡前的时段（留空不发）")
    jitter_minutes: int = Field(default=40, description="每个时段前后随机浮动多少分钟")
    probability: float = Field(default=0.5, description="到点后再判定一次：真的发的概率（0~1），调低可以减少风控")
    timeout: float = Field(default=300.0, description="写文案加画自拍的超时（秒）")


class RinneToolsConfig(PluginConfigBase):
    plugin: GeneralSection = Field(default_factory=GeneralSection)
    tools: ToolsSection = Field(default_factory=ToolsSection)
    qzone: QzoneSection = Field(default_factory=QzoneSection)


# ---------------------------------------------------------------- 消息解析


def _unwrap(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    if "message_info" in message:
        return message
    for key in ("message", "data", "result"):
        inner = message.get(key)
        if isinstance(inner, dict) and "message_info" in inner:
            return inner
    return {}


def _image_urls(raw: Any) -> list[str]:
    """从 MaiBot 的 raw_message 段列表里取图片地址。"""
    urls: list[str] = []
    stack = list(raw) if isinstance(raw, list) else [raw]
    while stack:
        seg = stack.pop(0)
        if isinstance(seg, list):
            stack[:0] = seg
            continue
        if not isinstance(seg, dict):
            continue
        data = seg.get("data")
        if seg.get("type") in {"image", "emoji"}:
            if isinstance(data, dict):
                url = data.get("url") or data.get("file")
            else:
                url = data
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
        elif isinstance(data, list | dict):
            stack.append(data)
    return urls


def _reply_target(raw: Any) -> str:
    """取消息里引用的那条消息的 ID（MaiBot 1.3 的 reply 段不带被引用消息的图片）。"""
    for seg in raw if isinstance(raw, list) else []:
        if isinstance(seg, dict) and seg.get("type") == "reply" and isinstance(seg.get("data"), dict):
            return str(seg["data"].get("target_message_id") or "").strip()
    return ""


def build_context(message: Any) -> dict[str, Any] | None:
    """把触发工具的消息转成 NoneBot 需要的上下文：谁、在哪个群、带了什么图。"""
    msg = _unwrap(message)
    info = msg.get("message_info") or {}
    user = info.get("user_info") or {}
    user_id = str(user.get("user_id") or "").strip()
    if not user_id or user_id == str(msg.get("account_id") or "").strip():
        return None
    group = info.get("group_info")
    group_id = str(group.get("group_id") or "").strip() if isinstance(group, dict) else ""
    message_id = str(msg.get("message_id") or "").strip()
    return {
        "user_id": user_id,
        "group_id": group_id or None,
        "user_name": str(user.get("user_cardname") or user.get("user_nickname") or ""),
        "message_id": message_id if message_id.isdigit() else None,
        "image_urls": _image_urls(msg.get("raw_message"))[:4],
    }


# ---------------------------------------------------------------- 工具描述（给凛音看）

# MaiBot 1.3 只把 description 和参数描述交给模型，detailed_description 不会出现在提示词里，
# 所以用法说明都写在这两处。「可以嘴上……但要真的做」是为了让人设里的傲娇不变成拒绝。
ACT_IN_CHARACTER = "可以按你的性格吐槽或嫌麻烦，但要真的调用它去做；只有工具返回失败时才说做不到。"

DRAW_DESC = (
    "画一张插画发到聊天里。对方请你画图、想要一张某某的图时调用。"
    f"{ACT_IN_CHARACTER}画你自己用 rinne_selfie。"
)
SELFIE_DESC = f"拍一张你自己（白鸟凛音）的自拍发到聊天里。对方想看你、要你的照片或自拍时调用。{ACT_IN_CHARACTER}"
TTS_DESC = (
    f"用语音说话发到聊天里。对方想听你的声音、让你用语音说、让你唱歌时调用。{ACT_IN_CHARACTER}"
    "唱歌和大段的话（讲故事、念长段落）只有主人（管理员）能点，别人点时工具会拒绝，你婉拒即可。"
    "说完不要再用文字重复同一句话。"
)
TTS_TEXT_DESC = (
    "要说的话，用你自己的口吻。平时一两句、不超过 100 字；主人要求时可以是大段的话或歌词。"
    "要写情感标记：在句子开头用半角方括号写当时的语气，例如 [happy]、[sad]、[angry]、[whispering]、"
    "[sighing]、[laughing]、[shy]、[impatient]，也可以写中文描述如 [冷淡又有点不耐烦]；"
    "停顿用 [break]、[long-break]。每句最多 3 个标记，要贴合人设和当下心情，不要每句都一样。"
    "唱歌时开头写 [singing]，后面是歌词。不要写（动作描写）、表情符号和波浪号"
)
DICE_DESC = "投骰子、做检定、抽签、帮忙随机决定时调用。点数由工具真随机产生并直接发到聊天里，你不能自己编点数，只需要点评。"
PIXIV_DESC = "在 Pixiv 上找插画发到聊天里。对方想看某个角色、某类题材的图时调用。"
DAILY_DESC = "从 Pixiv 挑一张今日推荐发到聊天里。对方想看看今天的图、每日一图时调用。"
QZONE_DESC = (
    "在你自己的 QQ 空间发一条说说，配一张你的自拍。有人明确让你发说说、发动态时调用；"
    "只有管理员（superuser）能让你发，你分不出是谁也照常调用，工具会核对身份，被拒绝时婉拒即可。"
    "说说由工具按你的人设写，写的是你自己的日常，不会提聊天内容。发出去要一两分钟，不要在聊天里写说说的内容。"
)
QZONE_TOPIC_DESC = "对方指定的题材，例如「今天下雪了」。没指定就不填，不要把聊天内容填进来"
QZONE_MOOD_DESC = "你现在的心情，两到六个字，例如「有点困」「心情不错」「被雨烦到」。会用在文案和自拍的表情上"

SCENE_DESC = (
    "自拍的情景。按聊天内容、现在的时间、你在工作中还是下班后，想一个自然的情景，"
    "写清楚在哪、在做什么、穿什么、表情和姿势。"
    "例：「下班后在房间里，穿着睡衣，侧马尾，抱着靠枕有点不耐烦地看镜头」。群聊里只能是工作中的样子"
)


def slot_times(cfg: QzoneSection) -> dict[str, str]:
    """时段名（NoneBot 按它决定写什么状态）→ 配置的时间。"""
    return {"work": cfg.work_time, "off_work": cfg.off_work_time, "bedtime": cfg.bedtime_time}


def plan_slots(day: date, times: dict[str, str], jitter_minutes: int,
               rng: random.Random) -> list[tuple[datetime, str]]:
    """排出某一天每个时段的触发时刻（前后随机浮动）。发不发到点再判定。"""
    slots = []
    jitter = max(0, jitter_minutes)
    for slot, raw in times.items():
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw or "").strip())
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            continue
        base = datetime(day.year, day.month, day.day, int(m[1]), int(m[2]), tzinfo=BEIJING)
        slots.append((base + timedelta(minutes=rng.uniform(-jitter, jitter)), slot))
    return sorted(slots)


def _msg_id_param() -> ToolParameterInfo:
    # NoneBot 要知道是谁在请求、结果引用哪条消息，所以每个工具都要模型填 msg_id
    return ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING,
                             description="对方提出这个请求的那条消息的 msg_id", required=True)


class RinneToolsPlugin(MaiBotPlugin):
    config_model = RinneToolsConfig

    _scheduler_task: asyncio.Task | None = None
    _jobs: set[asyncio.Task]
    _posting: asyncio.Lock

    async def on_load(self) -> None:
        self._jobs = set()
        self._posting = asyncio.Lock()
        self._restart_scheduler()
        self.ctx.logger.info("[凛音工具] 已加载")

    async def on_unload(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
        for task in list(getattr(self, "_jobs", ())):
            task.cancel()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._restart_scheduler()

    def _cfg(self) -> RinneToolsConfig:
        try:
            return self.config  # type: ignore[return-value]
        except RuntimeError:
            return RinneToolsConfig()

    async def _get_message(self, message_id: str, stream_id: str) -> Any:
        try:
            found = await self.ctx.message.get_by_id(message_id, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.warning("[凛音工具] 查询消息 %s 失败: %s", message_id, e)
            return None
        if isinstance(found, dict) and found.get("success") is False:
            self.ctx.logger.warning("[凛音工具] 查询消息 %s 失败: %s", message_id, found.get("error"))
            return None
        return found

    async def _resolve_context(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """找出是谁在请求。MaiBot 1.3 调用工具时不传消息本身，只能按模型给的 msg_id 去查。"""
        ctx = build_context(kwargs.get("message"))
        if ctx is not None:
            return ctx, ""

        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        msg_id = str(kwargs.get("msg_id") or "").strip()
        if msg_id:
            msg = _unwrap(await self._get_message(msg_id, stream_id))
            ctx = build_context(msg)
            if ctx is not None:
                target = _reply_target(msg.get("raw_message"))
                if not ctx["image_urls"] and target:
                    quoted = _unwrap(await self._get_message(target, stream_id))
                    ctx["image_urls"] = _image_urls(quoted.get("raw_message"))[:4]
                return ctx, ""

        user_id = str(kwargs.get("user_id") or "").strip()
        if user_id and not str(kwargs.get("group_id") or "").strip():
            # 私聊：聊天流本身就记录了对方是谁
            return {"user_id": user_id, "group_id": None, "user_name": "", "message_id": None, "image_urls": []}, ""
        if msg_id:
            return None, f"找不到消息 {msg_id}（会话 {stream_id or '未知'}），或发送者是机器人自己"
        return None, "群聊里模型没有填 msg_id"

    async def _call(self, tool: str, switch: str, args: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.plugin.enabled or not getattr(cfg.tools, switch, False):
            return {"success": False, "content": "这个功能现在没有开放，请用自己的话婉拒。"}
        if not cfg.plugin.token.strip():
            return {"success": False, "content": "工具还没有配置好（缺少 token），请告诉对方现在做不到。"}

        ctx, reason = await self._resolve_context(kwargs)
        if ctx is None:
            self.ctx.logger.warning("[凛音工具] %s 未执行：%s", tool, reason)
            return {"success": False, "content": "不知道是谁在请求。请在 msg_id 里填对方那条消息的 msg_id 后重新调用。"}

        args = {k: v for k, v in args.items() if v not in (None, "")}
        try:
            data = await self._request(tool, {**ctx, "args": args}, cfg.plugin.timeout)
        except (httpx.HTTPError, ValueError) as e:
            self.ctx.logger.warning("[凛音工具] 调用 %s 失败: %s", tool, e)
            return {"success": False, "content": "功能暂时出了点问题，请用自己的话告诉对方现在做不到。"}

        if not data.get("ok"):
            reason = str(data.get("message") or "未知原因")
            self.ctx.logger.info("[凛音工具] %s 被拒绝: %s", tool, reason)
            return {"success": False, "content": f"没能执行：{reason}。请用自己的口吻简短说明。"}

        self.ctx.logger.info("[凛音工具] %s 已开始（用户 %s）", tool, ctx["user_id"])
        result: dict[str, Any] = {"success": True, "content": str(data.get("content") or "已完成")}
        if data.get("stop_after_execution") is True:
            result["stop_after_execution"] = True
        return result

    async def _request(self, tool: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        cfg = self._cfg()
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{cfg.plugin.nonebot_url.rstrip('/')}/tools/{tool}",
                json=body,
                headers={"Authorization": f"Bearer {cfg.plugin.token.strip()}"},
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("NoneBot 返回格式不对")
        return data

    # -------------------------------------------------------- 说说

    async def _post_qzone(self, body: dict[str, Any]) -> str:
        """让 NoneBot 写文案、画自拍，再交给 Maizone 发到 QQ 空间。返回正文；失败抛 RuntimeError。"""
        async with self._posting:  # 同一时间只发一条，避免定时和手动撞在一起
            try:
                data = await self._request("qzone", body, self._cfg().qzone.timeout)
            except (httpx.HTTPError, ValueError) as e:
                raise RuntimeError(f"连不上 NoneBot：{e}") from e
            if not data.get("ok"):
                raise RuntimeError(str(data.get("message") or "NoneBot 没能写好说说"))
            text = str(data.get("text") or "").strip()
            try:
                image = base64.b64decode(str(data.get("image") or ""), validate=True)
            except ValueError as e:
                raise RuntimeError("NoneBot 返回的图片不对") from e
            if not text or not image:
                raise RuntimeError("NoneBot 没有返回正文或图片")
            try:
                result = await self.ctx.api.call(MAIZONE_API, message=text, images=[image])
            except Exception as e:
                raise RuntimeError(f"调用 Maizone 失败（装了 Maizone 插件吗？）：{e}") from e
            if isinstance(result, dict) and "result" in result:
                ok, detail = bool(result.get("result")), str(result.get("message") or "")
            elif isinstance(result, dict) and result.get("success") is False:
                ok, detail = False, str(result.get("error") or "")
            else:
                ok, detail = bool(result), ""
            if not ok:
                raise RuntimeError(f"Maizone 没发出去：{detail or '未知原因'}")
            self.ctx.logger.info("[凛音工具] 说说已发出：%s（%s）", text, detail)
            return text

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _qzone_job(self, body: dict[str, Any], stream_id: str) -> None:
        try:
            await self._post_qzone(body)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ctx.logger.warning("[凛音工具] 发说说失败：%s", e)
            if stream_id:
                await self.ctx.send.text(f"……说说没能发出去。（{e}）", stream_id)

    def _restart_scheduler(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        cfg = self._cfg()
        if not (cfg.plugin.enabled and cfg.tools.qzone and cfg.qzone.auto_enabled):
            return
        if not cfg.qzone.superuser_id.strip().isdigit():
            self.ctx.logger.warning("[凛音工具] 定时说说没有开启：没有填 superuser 的 QQ 号（superuser_id）")
            return
        self._scheduler_task = asyncio.get_running_loop().create_task(self._schedule_loop())

    async def _scheduled_post(self, slot: str, rng: random.Random) -> None:
        """到点后再判定一次发不发，通过了才让 NoneBot 按时段拟事件、写文案。"""
        cfg = self._cfg().qzone
        roll = rng.random()
        if roll >= cfg.probability:
            self.ctx.logger.info("[凛音工具] 定时说说（%s）这次不发（%.2f ≥ %.2f）", slot, roll, cfg.probability)
            return
        self.ctx.logger.info("[凛音工具] 定时说说（%s）判定通过，开始写", slot)
        try:
            await self._post_qzone({"user_id": cfg.superuser_id.strip(), "group_id": None, "args": {"slot": slot}})
        except Exception as e:
            self.ctx.logger.warning("[凛音工具] 定时说说失败：%s", e)

    async def _schedule_loop(self, rng: random.Random | None = None) -> None:
        rng = rng or random.Random()
        day = datetime.now(BEIJING).date()
        while True:
            cfg = self._cfg().qzone
            now = datetime.now(BEIJING)
            slots = [(t, s) for t, s in plan_slots(day, slot_times(cfg), cfg.jitter_minutes, rng) if t > now]
            self.ctx.logger.info("[凛音工具] %s 的定时说说时刻：%s", day,
                                 "、".join(f"{t:%H:%M}（{s}）" for t, s in slots) or "没有")
            for at, slot in slots:
                await asyncio.sleep(max(0.0, (at - datetime.now(BEIJING)).total_seconds()))
                await self._scheduled_post(slot, rng)
            day += timedelta(days=1)
            midnight = datetime(day.year, day.month, day.day, tzinfo=BEIJING)
            await asyncio.sleep(max(0.0, (midnight - datetime.now(BEIJING)).total_seconds()) + 5)

    # -------------------------------------------------------- 工具

    @Tool(
        "rinne_draw",
        brief_description=DRAW_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="prompt", param_type=ToolParamType.STRING, description="想要的画面：人物、动作、场景、氛围，中文即可", required=True),
            ToolParameterInfo(name="backend", param_type=ToolParamType.STRING, description="二次元插画选 novelai；写实照片、带文字、复杂构图选 gpt；不确定填 default",
                              required=False, enum_values=["default", "novelai", "gpt"], default="default"),
            ToolParameterInfo(name="size", param_type=ToolParamType.STRING, description="人物用竖图，风景用横图",
                              required=False, enum_values=["竖图", "横图", "方图"], default="竖图"),
            ToolParameterInfo(name="use_reference", param_type=ToolParamType.BOOLEAN,
                              description="对方发了图或引用了一张图，并让你照着画时设为 true", required=False, default=False),
        ],
        core_tool=True,
    )
    async def rinne_draw(self, prompt: str = "", backend: str = "default", size: str = "竖图",
                         use_reference: bool = False, **kwargs: Any) -> dict[str, Any]:
        return await self._call("draw", "draw", {
            "prompt": prompt, "backend": backend, "size": size, "use_reference": bool(use_reference),
        }, kwargs)

    @Tool(
        "rinne_selfie",
        brief_description=SELFIE_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="scene", param_type=ToolParamType.STRING, description=SCENE_DESC, required=True),
        ],
        core_tool=True,
    )
    async def rinne_selfie(self, scene: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._call("selfie", "selfie", {"scene": scene}, kwargs)

    @Tool(
        "rinne_voice",
        brief_description=TTS_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description=TTS_TEXT_DESC, required=True),
            ToolParameterInfo(name="sing", param_type=ToolParamType.BOOLEAN, description="对方让你唱歌时设为 true",
                              required=False, default=False),
        ],
        core_tool=True,
    )
    async def rinne_voice(self, text: str = "", sing: bool = False, **kwargs: Any) -> dict[str, Any]:
        return await self._call("tts", "tts", {"text": text, "sing": bool(sing) or None}, kwargs)

    @Tool(
        "rinne_dice",
        brief_description=DICE_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="faces", param_type=ToolParamType.INTEGER, description="骰子面数",
                              required=False, default=100),
            ToolParameterInfo(name="target", param_type=ToolParamType.INTEGER, description="检定的目标值，没有就不填", required=False),
            ToolParameterInfo(name="event", param_type=ToolParamType.STRING, description="检定的事件名，例如「侦查」「今天吃什么」", required=False),
        ],
        core_tool=True,
    )
    async def rinne_dice(self, faces: int = 100, target: int | None = None, event: str = "",
                         **kwargs: Any) -> dict[str, Any]:
        return await self._call("dice", "dice", {"faces": faces, "target": target, "event": event}, kwargs)

    @Tool(
        "rinne_pixiv_search",
        brief_description=PIXIV_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="tags", param_type=ToolParamType.STRING, description="搜索标签，日文或英文的作品名、角色名效果最好，例如「初音ミク」「原神」「風景」", required=True),
            ToolParameterInfo(name="count", param_type=ToolParamType.INTEGER, description="几张，1-3",
                              required=False, default=1),
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, description="random 随机高收藏，recent 最新，popular 热门",
                              required=False, enum_values=["random", "recent", "popular"], default="random"),
        ],
        core_tool=True,
    )
    async def rinne_pixiv_search(self, tags: str = "", count: int = 1, mode: str = "random",
                                 **kwargs: Any) -> dict[str, Any]:
        return await self._call("pixiv_search", "pixiv", {"tags": tags, "count": count, "mode": mode}, kwargs)

    @Tool("rinne_pixiv_daily", brief_description=DAILY_DESC, parameters=[_msg_id_param()], core_tool=True)
    async def rinne_pixiv_daily(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("pixiv_daily", "pixiv", {}, kwargs)

    @Tool(
        "rinne_qzone_post",
        brief_description=QZONE_DESC,
        parameters=[
            _msg_id_param(),
            ToolParameterInfo(name="topic", param_type=ToolParamType.STRING, description=QZONE_TOPIC_DESC, required=False),
            ToolParameterInfo(name="mood", param_type=ToolParamType.STRING, description=QZONE_MOOD_DESC, required=False),
        ],
        core_tool=True,
    )
    async def rinne_qzone_post(self, topic: str = "", mood: str = "", **kwargs: Any) -> dict[str, Any]:
        # 写文案加画图要一两分钟，先让 NoneBot 检查权限和配置，凛音能马上回话；真正的发送放到后台
        args = {"topic": topic, "mood": mood}
        checked = await self._call("qzone", "qzone", {**args, "check_only": True}, kwargs)
        if not checked["success"]:
            return checked
        ctx, _ = await self._resolve_context(kwargs)
        if ctx is None:
            return {"success": False, "content": "不知道是谁在请求。请在 msg_id 里填对方那条消息的 msg_id 后重新调用。"}
        body = {**ctx, "args": {k: v for k, v in args.items() if v}}
        self._spawn(self._qzone_job(body, str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")))
        return {
            "success": True,
            "content": "说说正在写、自拍正在画，一两分钟后会发到你的 QQ 空间。用你自己的口吻简短回应对方，"
                       "不要在聊天里写说说的内容。",
        }


def create_plugin():
    return RinneToolsPlugin()
