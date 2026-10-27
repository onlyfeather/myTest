from .._shared.help import HelpTopic, help_registry
from .._shared.services import fish_audio
from .config import tts_settings


def _detail() -> str:
    text = "/说 文字 - 让凛音读出来\n例：/说 欢迎回来，主人"
    if str(tts_settings.model) == fish_audio.DRAMA_MODEL:
        text += "\n\n可以用半角方括号指导语气：\n例：/说 [冷淡，有点不耐烦] 请您自重。[sigh]"
    return text + f"\n\n单次最多 {int(tts_settings.max_chars)} 个字，每人有冷却时间。"


help_registry.register(
    HelpTopic(
        key="tts",
        title="语音",
        aliases=("说", "语音合成", "朗读", "tts"),
        summary=("/说 文字 - 合成语音",),
        detail=_detail,
        admin=("管理员不受冷却限制；音色、模型在 /webui 里设置",),
        order=30,
        available=lambda: bool(tts_settings.enabled),
    )
)
