"""统一帮助：各插件在这里登记自己的指令，/帮助 汇总显示，/帮助 主题 显示详细说明。

本模块不依赖 NoneBot，方便单独测试。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HelpTopic:
    key: str
    title: str
    # 总览里每个主题列出的几条常用指令。
    summary: tuple[str, ...]
    # /帮助 主题 时显示的完整说明；为空时用 summary。传函数时每次显示都重新生成（内容随配置变化）。
    detail: str | Callable[[], str] = field(default="", compare=False)
    # 只有管理员能看到的指令，追加在详细说明末尾。
    admin: tuple[str, ...] = ()
    # /帮助 后面可以用来找到这个主题的词，key 和 title 自动包含。
    aliases: tuple[str, ...] = ()
    order: int = 100
    # 返回 False 时不显示（例如功能在 WebUI 里被关掉了）。
    available: Callable[[], bool] = field(default=lambda: True, compare=False)

    def names(self) -> set[str]:
        return {normalize(n) for n in (self.key, self.title, *self.aliases)}


def normalize(text: str) -> str:
    return text.strip().lower().removeprefix("/").replace(" ", "")


class HelpRegistry:
    def __init__(self) -> None:
        self._topics: dict[str, HelpTopic] = {}

    def register(self, topic: HelpTopic) -> None:
        taken = {name: t.key for t in self._topics.values() if t.key != topic.key for name in t.names()}
        clash = topic.names() & taken.keys()
        if clash:
            raise ValueError(f"帮助主题 {topic.key} 的名字 {sorted(clash)} 已被 {taken[next(iter(clash))]} 使用")
        self._topics[topic.key] = topic

    def topics(self) -> list[HelpTopic]:
        visible = [t for t in self._topics.values() if _safe(t.available)]
        return sorted(visible, key=lambda t: (t.order, t.key))

    def find(self, query: str) -> HelpTopic | None:
        q = normalize(query)
        if not q:
            return None
        topics = self.topics()
        for topic in topics:
            if q in topic.names():
                return topic
        # 允许输入指令本身，例如 /帮助 /nai、/帮助 .rd
        for topic in topics:
            if any(normalize(line.split()[0]) == q for line in topic.summary if line.split()):
                return topic
        return None

    def overview(self, *, is_admin: bool) -> str:
        lines = ["📖 指令帮助", ""]
        for topic in self.topics():
            lines.append(f"【{topic.title}】")
            lines.extend(topic.summary)
            lines.append("")
        lines.append("发送「/帮助 主题」查看详细说明，例如：" + "、".join(f"/帮助 {t.title}" for t in self.topics()[:3]))
        if is_admin:
            lines.append("（你是管理员，详细说明里会额外列出管理指令）")
        return "\n".join(lines).strip()

    def detail(self, topic: HelpTopic, *, is_admin: bool) -> str:
        detail = topic.detail() if callable(topic.detail) else topic.detail
        body = detail.strip() or "\n".join(topic.summary)
        text = f"📖 {topic.title}\n\n{body}"
        if is_admin and topic.admin:
            text += "\n\n🔧 管理指令（仅管理员）\n" + "\n".join(topic.admin)
        return text

    def not_found(self, query: str) -> str:
        names = "、".join(t.title for t in self.topics())
        return f"没有找到「{query.strip()}」的帮助。可以查看：{names}"


def _safe(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return True


help_registry = HelpRegistry()
