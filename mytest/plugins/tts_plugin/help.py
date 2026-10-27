from .._shared.help import HelpTopic, help_registry
from .._shared.services import fish_audio
from .config import tts_settings


def _detail() -> str:
    text = "/说 文字 - 让凛音读出来\n例：/说 欢迎回来，主人"
    if str(tts_settings.model) == fish_audio.DRAMA_MODEL:
        text += "\n\n可以用半角方括号指导语气：\n例：/说 [冷淡，有点不耐烦] 请您自重。[sigh]"
    text += f"\n\n单次最多 {int(tts_settings.max_chars)} 个字（方括号里的不算），每人有冷却时间。"
    return text + "\n\n不用指令也行：直接跟凛音说「用语音跟我说晚安」。"


help_registry.register(
    HelpTopic(
        key="tts",
        title="语音",
        aliases=("说", "语音合成", "朗读", "tts", "唱歌"),
        summary=("/说 文字 - 合成语音（也可以直接让凛音用语音说）",),
        detail=_detail,
        admin=(
            "唱歌：/说 [singing] 歌词，或直接让凛音唱；唱歌音色可以单独设置（/webui 选择音色）",
            "大段语音：管理员单次字数上限单独设置（/webui 语音合成 → 管理员单次最多字数）",
            "管理员不受冷却限制；音色、模型、字数在 /webui 里设置",
        ),
        order=30,
        available=lambda: bool(tts_settings.enabled),
    )
)
