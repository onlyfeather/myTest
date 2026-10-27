from nonebot.plugin import PluginMetadata

from .._shared.settings import settings_store
from . import handlers as handlers
from .service import test_gpt_image, test_novelai

settings_store.set_tester("novelai", test_novelai)
settings_store.set_tester("gpt_image", test_gpt_image)

__plugin_meta__ = PluginMetadata(
    name="ai_draw",
    description="AI 画图：NovelAI 和 GPT Image",
    usage="/画图 描述、/nai 描述、/gpt画图 描述；描述前加「横图」「方图」改尺寸",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
