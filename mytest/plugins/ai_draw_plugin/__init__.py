from nonebot import require

require("nonebot_plugin_localstore")
require("nonebot_plugin_waiter")

from nonebot.plugin import PluginMetadata

from .._shared.settings import settings_store
from . import handlers as handlers
from . import help as help  # noqa: A004  登记到统一帮助
from .service import list_gpt_image_models, test_gpt_image, test_novelai

settings_store.set_tester("novelai", test_novelai)
settings_store.set_tester("gpt_image", test_gpt_image)
settings_store.set_options_provider("gpt_image", "model", list_gpt_image_models)

__plugin_meta__ = PluginMetadata(
    name="ai_draw",
    description="AI 画图：NovelAI 和 GPT Image",
    usage="/画图 描述、/nai 描述、/gpt画图 描述；描述前加「横图」「方图」改尺寸",
    type="application",
    supported_adapters={"~onebot.v11"},
)

__all__ = ["handlers"]
