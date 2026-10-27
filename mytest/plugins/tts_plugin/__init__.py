from nonebot.plugin import PluginMetadata

from .._shared.settings import settings_store
from . import handlers as handlers
from .service import test_tts

settings_store.set_tester("tts", test_tts)

__plugin_meta__ = PluginMetadata(
    name="tts",
    description="Fish Audio 语音合成",
    usage="/说 要朗读的文字",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
