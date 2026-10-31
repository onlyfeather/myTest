"""mihomo（Clash Meta 内核）的控制接口：看代理组和节点、测延迟、切换节点、更新订阅。不依赖 NoneBot。

只调用 mihomo 的 external-controller（默认 9090 端口），不改 mihomo 的配置文件。
订阅要写在 mihomo 配置的 proxy-providers 里，这里只负责「立即更新」。
接口文档：https://wiki.metacubex.one/api/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .services.http import ServiceError, describe_exception

NAME = "mihomo"
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance"}
MANUAL = "Selector"  # 只有手动选择的组能切换节点；自动组（URLTest 等）由 mihomo 按延迟选
BUILTIN = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
DELAY_URL = "https://www.gstatic.com/generate_204"
DELAY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class Node:
    name: str
    type: str
    delay: int | None  # 最近一次测的延迟（毫秒）；None 没测过，0 测了但不通
    provider: str = ""


@dataclass(frozen=True)
class Group:
    name: str
    type: str
    now: str
    members: tuple[str, ...]

    @property
    def manual(self) -> bool:
        return self.type == MANUAL


@dataclass(frozen=True)
class Provider:
    name: str
    vehicle: str  # HTTP（订阅）/ File / Inline
    count: int
    updated_at: str
    used: int = 0  # 订阅流量信息（有的话）
    total: int = 0
    expire: int = 0


@dataclass
class Snapshot:
    groups: list[Group] = field(default_factory=list)
    nodes: dict[str, Node] = field(default_factory=dict)
    providers: list[Provider] = field(default_factory=list)


def _last_delay(item: dict[str, Any]) -> int | None:
    history = item.get("history") or []
    if not history:
        return None
    delay = history[-1].get("delay")
    return int(delay) if isinstance(delay, (int, float)) else None


def parse_proxies(data: dict[str, Any]) -> tuple[list[Group], dict[str, Node]]:
    groups, nodes = [], {}
    for name, item in (data.get("proxies") or {}).items():
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in GROUP_TYPES:
            if name == "GLOBAL" or item.get("hidden"):
                continue
            groups.append(Group(name, kind, str(item.get("now") or ""), tuple(item.get("all") or ())))
        elif name not in BUILTIN:
            nodes[name] = Node(name, kind, _last_delay(item), str(item.get("provider-name") or ""))
    return groups, nodes


def parse_providers(data: dict[str, Any]) -> list[Provider]:
    out = []
    for name, item in (data.get("providers") or {}).items():
        if not isinstance(item, dict) or name == "default":
            continue
        vehicle = str(item.get("vehicleType") or "")
        if vehicle == "Compatible":
            continue
        info = item.get("subscriptionInfo") or {}
        out.append(Provider(
            name=name, vehicle=vehicle, count=len(item.get("proxies") or []),
            updated_at=str(item.get("updatedAt") or ""),
            used=int(info.get("Upload") or 0) + int(info.get("Download") or 0),
            total=int(info.get("Total") or 0), expire=int(info.get("Expire") or 0),
        ))
    return out


class Mihomo:
    def __init__(self, url: str, secret: str = "", *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = url.strip().rstrip("/")
        self.secret = secret.strip()
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        # 控制接口在内网，永远直连，不走代理（不然切坏节点后就连不上控制接口了）
        return httpx.AsyncClient(base_url=self.base, headers=headers, transport=self.transport, trust_env=False,
                                 timeout=httpx.Timeout(self.timeout, connect=5.0))

    async def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> Any:
        if not self.base:
            raise ServiceError("还没有填 mihomo 控制接口的地址")
        try:
            async with self._client() as client:
                resp = await client.request(method, path, timeout=timeout or self.timeout, **kwargs)
        except httpx.ConnectError as e:
            raise ServiceError(
                f"{NAME} 控制接口连不上（{self.base}）。检查 mihomo 配置里的 external-controller 是不是 0.0.0.0:9090，"
                f"以及这里填的地址：{describe_exception(NAME, e)}") from None
        except httpx.HTTPError as e:
            raise ServiceError(describe_exception(NAME, e)) from None
        if resp.status_code == 401:
            raise ServiceError(f"{NAME} 控制接口的密钥不对：和 mihomo 配置里的 secret 填成一样的")
        if resp.status_code == 404:
            raise ServiceError(f"{NAME} 找不到这一项（{path}），可能已经被删掉了，刷新一下")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("message") or "")
            except ValueError:
                detail = resp.text[:100]
            raise ServiceError(f"{NAME} 返回 HTTP {resp.status_code}" + (f"：{detail}" if detail else ""))
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ServiceError(f"{NAME} 返回的不是 JSON，检查地址是不是控制接口（一般是 9090 端口）") from None

    async def version(self) -> str:
        data = await self._request("GET", "/version")
        return str((data or {}).get("version") or "未知版本")

    async def snapshot(self) -> Snapshot:
        groups, nodes = parse_proxies(await self._request("GET", "/proxies") or {})
        providers = parse_providers(await self._request("GET", "/providers/proxies") or {})
        return Snapshot(groups, nodes, providers)

    async def select(self, group: str, node: str) -> None:
        await self._request("PUT", f"/proxies/{quote(group, safe='')}", json={"name": node})

    async def test_group(self, group: str, url: str = DELAY_URL, timeout_ms: int = DELAY_TIMEOUT_MS) -> dict[str, int]:
        """测一整组节点的延迟，返回 {节点: 毫秒}；不通的节点不在结果里。"""
        data = await self._request("GET", f"/group/{quote(group, safe='')}/delay",
                                   params={"url": url, "timeout": timeout_ms}, timeout=timeout_ms / 1000 + 30)
        return {str(k): int(v) for k, v in (data or {}).items() if isinstance(v, (int, float))}

    async def update_provider(self, name: str) -> None:
        # 拉订阅可能比较慢
        await self._request("PUT", f"/providers/proxies/{quote(name, safe='')}", timeout=60)


# ---------------------------------------------------------------- 给 WebUI 的列表


def fmt_delay(delay: int | None) -> str:
    if delay is None:
        return "未测速"
    if delay <= 0:
        return "不通"
    return f"{delay} ms"


def fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


MAX_DELAYS_SHOWN = 8


def delay_counts(group: Group, snap: Snapshot, result: dict[str, int]) -> str:
    """测速结果里真正的节点有几个能用、几个不通（组里的子组和 DIRECT 这类内置项不算）。"""
    real = [m for m in group.members if m in snap.nodes and m not in BUILTIN]
    ok = sum(1 for m in real if result.get(m))
    return f"{ok} 个能用，{len(real) - ok} 个不通"


def delay_summary(g: Group, nodes: dict[str, Node]) -> str:
    """组里测过速的节点，按延迟从快到慢列几个。"""
    tested = [(nodes[m].delay, m) for m in g.members if m in nodes and nodes[m].delay]
    tested.sort()
    dead = sum(1 for m in g.members if m in nodes and nodes[m].delay == 0)
    parts = [f"{m} {d} ms" for d, m in tested[:MAX_DELAYS_SHOWN]]
    if len(tested) > MAX_DELAYS_SHOWN:
        parts.append(f"…还有 {len(tested) - MAX_DELAYS_SHOWN} 个")
    if dead:
        parts.append(f"{dead} 个不通")
    return "；".join(parts)


def items(snap: Snapshot, expire_text=None) -> list[dict[str, Any]]:
    """WebUI 列表：每个代理组一行（手动组带「切换节点」），每个订阅一行（带「立即更新」）。"""
    out = []
    for g in snap.groups:
        current = snap.nodes.get(g.now)
        detail = f"当前：{g.now or '无'}"
        if current:
            detail += f"（{fmt_delay(current.delay)}）"
        detail += f"；共 {len(g.members)} 项"
        delays = delay_summary(g, snap.nodes)
        if delays:
            detail += f"\n延迟：{delays}"
        kind = "手动选择" if g.manual else {"URLTest": "自动选最快", "Fallback": "故障转移",
                                            "LoadBalance": "负载均衡"}.get(g.type, g.type)
        out.append({
            "id": f"g:{g.name}", "title": g.name, "detail": detail, "badges": ["代理组", kind],
            # 自动选择的组由 mihomo 自己挑节点，只能测速
            "actions": ["select", "test"] if g.manual else ["test"],
            # 「切换节点」的下拉框：第一项是当前节点
            "values": {"select": [g.now] + [m for m in g.members if m != g.now] if g.now else list(g.members)},
        })
    for p in snap.providers:
        detail = f"{p.count} 个节点"
        if p.updated_at:
            detail += f"；上次更新：{p.updated_at[:19].replace('T', ' ')}"
        if p.total:
            detail += f"；流量 {fmt_bytes(p.used)} / {fmt_bytes(p.total)}"
        if p.expire and expire_text:
            detail += f"；到期：{expire_text(p.expire)}"
        out.append({"id": f"p:{p.name}", "title": p.name, "detail": detail,
                    "badges": ["订阅" if p.vehicle == "HTTP" else "节点文件"],
                    "actions": ["update"] if p.vehicle == "HTTP" else []})
    return out


def pick_node(value: Any, group: Group) -> str:
    """下拉框选中的节点名，必须是这个组里的。"""
    name = str(value or "").strip()
    if not name:
        raise ValueError("请选择一个节点")
    if name not in group.members:
        raise ValueError(f"「{name[:40]}」不在「{group.name}」里，刷新一下再选")
    return name
