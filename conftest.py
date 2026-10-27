"""pytest 在导入任何插件之前先初始化 NoneBot（插件包的 __init__ 里会调用 require）。

运行全部测试：python -m pytest --import-mode=importlib tests mytest -q
"""

import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parent))
nonebot.init(log_level="INFO")
