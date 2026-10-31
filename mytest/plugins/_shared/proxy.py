"""网络代理：在 WebUI「网络代理」页统一配置代理地址，各服务各自选择怎么连。

每个服务（大模型、画图、语音合成）的「连接方式」：
- auto（默认）：老行为。服务自己的「代理」填了就用它；没填就按容器环境变量（HTTP_PROXY / HTTPS_PROXY），
  没有环境变量就直连。升级后什么都不用改。
- direct：真正直连，连环境变量里的代理也不用（国内能直连的中转站选这个）。
- main：用「网络代理」页的公共代理。
- backup：用「网络代理」页的备用代理（主代理不稳定时可以切过去）。
- custom：用服务自己的「代理」。

本模块不依赖 NoneBot（测试里直接构造 SettingsStore）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from typing import TYPE_CHECKING

# 配置项的类在用到时才导入：settings 包的 __init__ 会导入本模块，模块顶上导入 settings.registry
# 会在先导入本模块时（比如单独跑脚本）形成循环导入
if TYPE_CHECKING:
    from .settings.registry import SettingField, SettingSection

AUTO, DIRECT, MAIN, BACKUP, CUSTOM, INHERIT = "auto", "direct", "main", "backup", "custom", "inherit"
# resolve 返回它表示「真正直连」：连环境变量里的 HTTP_PROXY / HTTPS_PROXY 也不用。
# 空字符串还是老意思：没指定代理，按环境变量来（docker-compose.yml 里设的）。
NO_PROXY = "direct://"
MODES = (
    (AUTO, "跟以前一样（填了下面的代理就用，没填就按容器的环境变量）"),
    (DIRECT, "直连（不走任何代理，包括容器环境变量里的）"),
    (MAIN, "用「网络代理」页的公共代理"),
    (BACKUP, "用「网络代理」页的备用代理"),
    (CUSTOM, "用下面单独填的代理"),
)
# 只支持 HTTP 代理：httpx 用 SOCKS 代理要另装 socksio，项目没有装
SCHEMES = ("http", "https")

# 各服务的「测试连接」用这些地址，只看能不能连上，不发任何数据
PROBES = (
    ("大模型（OpenAI）", "https://api.openai.com"),
    ("Fish Audio", "https://api.fish.audio"),
    ("NovelAI", "https://image.novelai.net"),
    ("Pixiv", "https://www.pixiv.net"),
)


def section() -> SettingSection:
    from .settings.registry import SettingField, SettingSection

    return SettingSection(
        id="proxy",
        title="网络代理",
        description="在这里填一次代理地址，「大模型」「画图」「语音合成」里把「连接方式」选成公共代理或备用代理就能用上；"
                    "各服务原来单独填的代理继续有效（连接方式默认是「跟以前一样」）。"
                    "填了 mihomo 控制接口后，页面下方可以测速、切换节点、更新订阅。",
        order=5,
        fields=[
            SettingField("main", "公共代理", default="", placeholder="http://mihomo:7890", validate=problem,
                         help="http:// 开头，带端口（只支持 HTTP 代理）。Docker 里填代理容器的名字，例如 http://mihomo:7890；"
                              "代理在宿主机上就填宿主机的内网地址，不能填 127.0.0.1（那是容器自己）"),
            SettingField("backup", "备用代理", default="", placeholder="http://mihomo:7891", validate=problem,
                         help="另一个节点或另一个代理。主代理不稳定时，把服务的连接方式改成「备用代理」就行，不用改地址"),
            SettingField("mihomo_url", "mihomo 控制接口", default="", placeholder="http://mihomo:9090",
                         group="mihomo", validate=controller_problem,
                         help="填了就能在下面查看节点、测速、切换节点、更新订阅。mihomo 配置里的 external-controller "
                              "要改成 0.0.0.0:9090（默认的 127.0.0.1 只有它自己能连）"),
            SettingField("mihomo_secret", "控制接口密钥", type="secret", default="", group="mihomo",
                         help="和 mihomo 配置里的 secret 一样。控制接口开放给其他容器时一定要设"),
        ],
    )


def mode_field(group: str = "连接") -> SettingField:
    from .settings.registry import SettingField

    return SettingField("proxy_mode", "连接方式", type="select", default=AUTO, options=MODES, group=group,
                        help="国内服务器连国外接口一般要走代理。代理地址在左侧「网络代理」页统一填。"
                             "docker-compose.yml 里设了 HTTP_PROXY 时，「跟以前一样」会走它，「直连」不会")


def inherit_mode_field(parent: str, group: str = "连接") -> SettingField:
    """子服务（比如 NovelAI、GPT Image）的连接方式：默认跟上级（「AI 画图」）一样，也可以单独选。"""
    from .settings.registry import SettingField

    options = ((INHERIT, f"跟「{parent}」一样"),
               *((v, label) for v, label in MODES if v != AUTO))
    return SettingField("proxy_mode", "连接方式", type="select", default=INHERIT, options=options, group=group,
                        help=f"默认用「{parent}」页的设置。国内能直连的中转站选「直连」，不用绕代理"
                             "（docker-compose.yml 里的 HTTP_PROXY 也不会用）")


def custom_field(help: str = "", group: str = "连接") -> SettingField:
    """服务自己的代理。键名还是 proxy，原来保存的值继续有效。"""
    from .settings.registry import SettingField

    return SettingField("proxy", "单独的代理", default="", placeholder="http://mihomo:7890", group=group,
                        validate=problem,
                        help=help or "连接方式选「用下面单独填的代理」或「跟以前一样」时生效；留空表示直连")


def resolve(mode: str, custom: str, main: str, backup: str) -> str:
    """返回要用的代理地址；空字符串表示直连。inherit 要先由调用方换成上级的结果（见 settings.proxy_for）。"""
    custom, main, backup = (str(x or "").strip() for x in (custom, main, backup))
    if mode == DIRECT:
        return NO_PROXY
    if mode == MAIN:
        return main or NO_PROXY  # 选了公共代理却没填：直连，不要悄悄走环境变量
    if mode == BACKUP:
        return backup or NO_PROXY
    if mode == CUSTOM:
        return custom or NO_PROXY
    return custom  # auto：服务自己的代理；没填就按环境变量（老行为）


def environment() -> dict[str, str]:
    """容器环境变量里的代理设置，给 WebUI 看（去掉用户名密码）。"""
    import os

    out = {}
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = os.environ.get(key) or os.environ.get(key.lower())
        if value:
            out[key] = value if key == "NO_PROXY" else describe(value)
    return out


def problem(url: str) -> str:
    """代理地址有什么问题；没问题返回空字符串。"""
    url = str(url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme.startswith("socks"):
        return "暂不支持 SOCKS 代理，请填代理的 HTTP 端口（mihomo / Clash 一般是 7890）"
    if parts.scheme not in SCHEMES:
        return "要以 http:// 或 https:// 开头"
    try:
        port = parts.port
    except ValueError:
        return "端口不对"
    if not parts.hostname:
        return "缺少主机名"
    if port is None:
        return "要写端口，例如 http://mihomo:7890"
    return ""


def controller_problem(url: str) -> str:
    """mihomo 控制接口地址：http(s):// 开头，有主机名。"""
    url = str(url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "要以 http:// 开头，例如 http://mihomo:9090"
    if not parts.hostname:
        return "缺少主机名，例如 http://mihomo:9090"
    try:
        _ = parts.port  # 端口不是数字或超出范围时抛 ValueError
    except ValueError:
        return "端口不对"
    return ""


def describe(url: str) -> str:
    """日志和页面上显示的代理：去掉用户名密码。"""
    url = str(url or "").strip()
    if url == NO_PROXY:
        return "直连"
    if not url:
        return "按容器环境变量"
    parts = urlsplit(url)
    host = parts.hostname or "?"
    return f"{parts.scheme}://{host}:{parts.port}" if parts.port else f"{parts.scheme}://{host}"


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str
    ms: int


async def probe(proxy: str, targets: tuple[tuple[str, str], ...] = PROBES, timeout: float = 10.0,
                clock: Callable[[], float] = time.monotonic) -> list[Probe]:
    """通过这个代理（或直连）依次连一下每个地址。任何 HTTP 状态码都算连上了。"""
    from .services.http import describe_exception, http_client

    results = []
    async with http_client(proxy, timeout) as client:
        for name, url in targets:
            started = clock()
            try:
                resp = await client.get(url)
            except Exception as e:
                results.append(Probe(name, False, describe_exception(name, e), int((clock() - started) * 1000)))
                continue
            results.append(Probe(name, True, f"HTTP {resp.status_code}", int((clock() - started) * 1000)))
    return results


def report(label: str, proxy: str, results: list[Probe]) -> str:
    lines = [f"{label}（{describe(proxy)}）："]
    for r in results:
        lines.append(f"  {'✓' if r.ok else '✗'} {r.name}：{r.detail}（{r.ms} ms）")
    return "\n".join(lines)
