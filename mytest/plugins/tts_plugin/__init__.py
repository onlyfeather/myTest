from nonebot.plugin import PluginMetadata

from .._shared.settings import settings_store
from . import handlers as handlers
from . import help as help  # noqa: A004  登记到统一帮助
from .service import list_voices, test_tts

settings_store.set_tester("tts", test_tts)
settings_store.set_options_provider("tts", "reference_id", list_voices)

__plugin_meta__ = PluginMetadata(
    name="tts",
    description="Fish Audio 语音合成",
    usage="/说 要朗读的文字",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
