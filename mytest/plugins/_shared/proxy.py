"""网络代理：在 WebUI「网络代理」页统一配置代理地址，各服务各自选择怎么连。

每个服务（大模型、画图、语音合成）的「连接方式」：
- auto（默认）：老行为。服务自己的「代理」填了就用它，没填就直连。升级后什么都不用改。
- direct：直连，忽略所有代理。
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

from .settings.registry import SettingField, SettingSection

AUTO, DIRECT, MAIN, BACKUP, CUSTOM = "auto", "direct", "main", "backup", "custom"
MODES = (
    (AUTO, "跟以前一样（填了下面的代理就用，没填就直连）"),
    (DIRECT, "直连"),
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
    return SettingSection(
        id="proxy",
        title="网络代理",
        description="在这里填一次代理地址，「大模型」「画图」「语音合成」里把「连接方式」选成公共代理或备用代理就能用上。"
                    "各服务原来单独填的代理继续有效（连接方式默认是「跟以前一样」）。",
        order=5,
        fields=[
            SettingField("main", "公共代理", default="", placeholder="http://mihomo:7890", validate=problem,
                         help="http:// 开头，带端口（只支持 HTTP 代理）。Docker 里填代理容器的名字，例如 http://mihomo:7890；"
                              "代理在宿主机上就填宿主机的内网地址，不能填 127.0.0.1（那是容器自己）"),
            SettingField("backup", "备用代理", default="", placeholder="http://mihomo:7891", validate=problem,
                         help="另一个节点或另一个代理。主代理不稳定时，把服务的连接方式改成「备用代理」就行，不用改地址"),
        ],
    )


def mode_field(group: str = "连接") -> SettingField:
    return SettingField("proxy_mode", "连接方式", type="select", default=AUTO, options=MODES, group=group,
                        help="国内服务器连国外接口一般要走代理。代理地址在左侧「网络代理」页统一填")


def custom_field(help: str = "", group: str = "连接") -> SettingField:
    """服务自己的代理。键名还是 proxy，原来保存的值继续有效。"""
    return SettingField("proxy", "单独的代理", default="", placeholder="http://mihomo:7890", group=group,
                        validate=problem,
                        help=help or "连接方式选「用下面单独填的代理」或「跟以前一样」时生效；留空表示直连")


def resolve(mode: str, custom: str, main: str, backup: str) -> str:
    """返回要用的代理地址；空字符串表示直连。"""
    custom, main, backup = (str(x or "").strip() for x in (custom, main, backup))
    if mode == DIRECT:
        return ""
    if mode == MAIN:
        return main
    if mode == BACKUP:
        return backup
    return custom  # auto 和 custom：服务自己的代理，没填就直连


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


def describe(url: str) -> str:
    """日志和页面上显示的代理：去掉用户名密码。"""
    url = str(url or "").strip()
    if not url:
        return "直连"
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
