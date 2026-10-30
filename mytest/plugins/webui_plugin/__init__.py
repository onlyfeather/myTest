"""插件配置 WebUI，挂在 NoneBot 自带的 FastAPI 上：http://服务器:端口/webui

.env 里需要设置：
    WEBUI_PASSWORD=一个足够长的随机密码      # 不设置就不开放
    WEBUI_SECURE_COOKIE=true               # 可选，用 HTTPS 反代访问时打开
"""

import os
import time

from nonebot import get_app, get_bots, get_driver, logger
from nonebot.drivers import ASGIMixin
from nonebot.plugin import PluginMetadata

from .._shared.services.llm import list_models, test_connection
from .._shared.settings import settings_store
from .._shared.status import STARTED_AT, StatusRow, duration, full_snapshot, install_log_sink, status_registry
from .server import create_router

__plugin_meta__ = PluginMetadata(
    name="webui",
    description="插件配置网页",
    usage="浏览器打开 http://服务器:端口/webui",
    type="application",
)

MIN_PASSWORD_LENGTH = 12

settings_store.set_tester("llm", test_connection)
settings_store.set_options_provider("llm", "model", list_models)


def _system_status() -> list[StatusRow]:
    bots = list(get_bots())
    return [
        StatusRow("QQ 机器人", f"在线：{'、'.join(bots)}" if bots else "不在线（NapCat 没有连上）",
                  "ok" if bots else "error"),
        StatusRow("已运行", duration(time.time() - STARTED_AT)),
    ]


status_registry.register("system", "系统", _system_status, order=0)
install_log_sink()


def _config_value(name: str) -> str:
    # NoneBot 只把工作目录下 .env 文件里的未声明配置放进 config；
    # docker-compose 用 env_file / environment 传进来的只是环境变量，要单独读。
    value = getattr(get_driver().config, name, None)
    if value in (None, ""):
        value = os.environ.get(name.upper(), "")
    return str(value or "").strip()


def _disabled_reason() -> str:
    raw = _config_value("webui_password")
    if not raw:
        return "WebUI 未启用：没有读到 WEBUI_PASSWORD，请在 .env 里设置后重启"
    if len(raw) < MIN_PASSWORD_LENGTH:
        return f"WebUI 未启用：WEBUI_PASSWORD 只有 {len(raw)} 位，至少需要 {MIN_PASSWORD_LENGTH} 位"
    return ""


def _password() -> str:
    return "" if _disabled_reason() else _config_value("webui_password")


def _secure_cookie() -> bool:
    return _config_value("webui_secure_cookie").lower() in {"1", "true", "yes", "on"}


driver = get_driver()
if isinstance(driver, ASGIMixin):
    get_app().include_router(
        create_router(
            settings_store, _password, secure_cookie=_secure_cookie, disabled_reason=_disabled_reason,
            status=full_snapshot,
        )
    )
    if reason := _disabled_reason():
        logger.warning("[WebUI] {}", reason)
    else:
        logger.info("[WebUI] 配置页面已开启：/webui，配置文件 {}", settings_store.path)
else:
    logger.warning("[WebUI] 当前驱动器不支持 HTTP 服务，配置页面不可用")
