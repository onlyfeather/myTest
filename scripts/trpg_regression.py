"""用真模型跑跑团提示词的回归用例。读 WebUI 里保存的模型配置，和真实游戏用同一套。

在 NoneBot 的容器里、项目根目录下运行：
    python scripts/trpg_regression.py            # 全部用例
    python scripts/trpg_regression.py 用例名 ...  # 只跑这几个

插件包导入时要先初始化 NoneBot，所以不能直接 python -m 跑插件里的模块。
"""

import sys
from pathlib import Path

import nonebot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

nonebot.init(log_level="WARNING")
nonebot.load_plugin("nonebot_plugin_localstore")

from mytest.plugins.trpg_plugin.regression import main  # noqa: E402

main()
