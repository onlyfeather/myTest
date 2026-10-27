from nonebot.plugin import PluginMetadata

from .._shared.settings import Picker, settings_store
from . import handlers as handlers
from . import help as help  # noqa: A004  登记到统一帮助
from .service import search_voices, test_tts

settings_store.set_tester("tts", test_tts)
settings_store.add_picker(
    Picker(
        id="voices",
        title="选择音色",
        section_id="tts",
        targets=(("reference_id", "设为说话音色"), ("singing_reference_id", "设为唱歌音色")),
        search=search_voices,
        description="搜索 Fish Audio 音色库，试听后设为说话或唱歌音色，立即生效。",
    )
)

__plugin_meta__ = PluginMetadata(
    name="tts",
    description="Fish Audio 语音合成",
    usage="/说 要朗读的文字",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
