"""凛音的工具：把 NoneBot 的功能交给凛音在聊天中使用。

工具本身不在这里实现，而是调用同一台服务器上 NoneBot 的 /tools 接口。
NoneBot 负责执行、限流、发确认词，并把结果（图片、语音）直接发到聊天里；
这里只把「谁、在哪、要做什么」传过去，再把给凛音看的说明返回给 MaiBot。
"""

from __future__ import annotations

from typing import Any

import httpx
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

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


class RinneToolsConfig(PluginConfigBase):
    plugin: GeneralSection = Field(default_factory=GeneralSection)
    tools: ToolsSection = Field(default_factory=ToolsSection)


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
    f"用语音说一句话发到聊天里。对方想听你的声音、让你用语音说时调用。{ACT_IN_CHARACTER}"
    "说完不要再用文字重复同一句话。"
)
DICE_DESC = "投骰子、做检定、抽签、帮忙随机决定时调用。点数由工具真随机产生并直接发到聊天里，你不能自己编点数，只需要点评。"
PIXIV_DESC = "在 Pixiv 上找插画发到聊天里。对方想看某个角色、某类题材的图时调用。"
DAILY_DESC = "从 Pixiv 挑一张今日推荐发到聊天里。对方想看看今天的图、每日一图时调用。"

SCENE_DESC = (
    "自拍的情景。按聊天内容、现在的时间、你在工作中还是下班后，想一个自然的情景，"
    "写清楚在哪、在做什么、穿什么、表情和姿势。"
    "例：「下班后在房间里，穿着睡衣，侧马尾，抱着靠枕有点不耐烦地看镜头」。群聊里只能是工作中的样子"
)


def _msg_id_param() -> ToolParameterInfo:
    # NoneBot 要知道是谁在请求、结果引用哪条消息，所以每个工具都要模型填 msg_id
    return ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING,
                             description="对方提出这个请求的那条消息的 msg_id", required=True)


class RinneToolsPlugin(MaiBotPlugin):
    config_model = RinneToolsConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("[凛音工具] 已加载")

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        pass

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
        url = f"{cfg.plugin.nonebot_url.rstrip('/')}/tools/{tool}"
        try:
            async with httpx.AsyncClient(timeout=cfg.plugin.timeout, trust_env=False) as client:
                resp = await client.post(
                    url,
                    json={**ctx, "args": args},
                    headers={"Authorization": f"Bearer {cfg.plugin.token.strip()}"},
                )
            data = resp.json()
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
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="要说的话，用你自己的口吻，一两句，不超过 100 字，不写动作描写", required=True),
        ],
        core_tool=True,
    )
    async def rinne_voice(self, text: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._call("tts", "tts", {"text": text}, kwargs)

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


def create_plugin():
    return RinneToolsPlugin()
