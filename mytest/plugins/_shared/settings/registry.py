"""配置注册表：插件声明配置项，WebUI 读写，插件运行时随时读取最新值。

取值优先级：WebUI 保存的值 > 环境变量 / .env > 默认值。
本模块不依赖 NoneBot，方便单独测试。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIELD_TYPES = {"text", "secret", "textarea", "lines", "bool", "int", "float", "select"}
OptionsProvider = Callable[[], Awaitable[list[tuple[str, str]]]]
MAX_FETCHED_OPTIONS = 500


@dataclass
class FileSlot:
    """WebUI 里可以上传的一个文件（目前只用于图片）。"""

    key: str
    label: str
    path: Callable[[], Path]  # 文件位置
    save: Callable[[bytes], str]  # 校验并保存，返回给用户看的说明；不合法时抛 ValueError
    help: str = ""
    max_bytes: int = 20 * 1024 * 1024


@dataclass
class Picker:
    """WebUI 里单独的一页：搜索外部资源（比如音色库），选中后写进某个分区的配置项。

    search(query=, page=, mine=, language=) 返回 {"items": [{value, title, detail, sample}], "total", "has_more"}。
    """

    id: str
    title: str
    section_id: str
    targets: tuple[tuple[str, str], ...]  # (配置项, 显示文字)
    search: Callable[..., Awaitable[dict[str, Any]]]
    description: str = ""


MAX_PICKER_ITEMS = 100
_TRUE_WORDS = {"1", "true", "yes", "on", "开", "开启", "启用"}
_FALSE_WORDS = {"0", "false", "no", "off", "关", "关闭", "停用", ""}


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    type: str = "text"
    default: Any = ""
    help: str = ""
    env: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()  # (值, 显示文字)
    minimum: float | None = None
    maximum: float | None = None
    group: str = ""
    placeholder: str = ""
    # 为 True 时 WebUI 显示「获取列表」按钮，可选项由 set_options_provider 注册的函数提供。
    fetchable: bool = False

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"未知的配置类型: {self.type}")


@dataclass
class AccountPanel:
    """WebUI 里的「账号」卡片：列出几个平台账号的登录状态，可以扫码登录、退出。

    status() 返回 [{platform, name, logged_in, expired, message, account_name, vip, login_kinds}]。
    qr_start(platform, kind) 返回 {sid, image, hint}；qr_poll(sid) 返回 {status, message, account?}。
    logout(platform) 退出登录。都不能返回凭证本身。
    """

    title: str
    help: str
    status: Callable[[], list[dict[str, Any]]]
    qr_start: Callable[[str, str], Awaitable[dict[str, Any]]]
    qr_poll: Callable[[str], Awaitable[dict[str, Any]]]
    logout: Callable[[str], None]


MANAGER_SCOPES = {"item", "global"}
MANAGER_INPUTS = {"", "text", "lines"}
MAX_MANAGER_ITEMS = 5000
MAX_ACTION_TEXT = 5000
MAX_ACTION_LINES = 200


@dataclass(frozen=True)
class ManagerAction:
    """列表管理卡片上的一个按钮。"""

    name: str
    label: str
    scope: str = "item"  # item：每一行一个按钮；global：列表上方
    input: str = ""  # 空：直接执行；text：一个输入框；lines：多行（item 的按钮用 item["values"][name] 预填）
    placeholder: str = ""
    confirm: str = ""  # 不为空时先弹确认框，{title} 换成这一行的标题
    primary: bool = False

    def __post_init__(self) -> None:
        if self.scope not in MANAGER_SCOPES or self.input not in MANAGER_INPUTS:
            raise ValueError(f"按钮 {self.name} 的 scope 或 input 不正确")


@dataclass
class ItemManager:
    """WebUI 里的「列表管理」卡片（比如 Pixiv 关注的作者）：列出条目，增删改由插件处理。

    items() 返回 {"items": [{id, title, detail, link, badges, values}], "notice": "提示"}。
    run(action, item_id, value) 执行按钮，返回给用户看的结果；用户填错时抛 ValueError。
    """

    title: str
    help: str
    items: Callable[[], Awaitable[dict[str, Any]]]
    actions: tuple[ManagerAction, ...]
    run: Callable[[str, str, Any], Awaitable[str]]

    def action(self, name: str) -> ManagerAction:
        for item in self.actions:
            if item.name == name:
                return item
        raise KeyError(f"没有 {name} 这个操作")

    def describe(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "help": self.help,
            "actions": [
                {"name": a.name, "label": a.label, "scope": a.scope, "input": a.input,
                 "placeholder": a.placeholder, "confirm": a.confirm, "primary": a.primary}
                for a in self.actions
            ],
        }


def _clean_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
        return None
    link = str(raw.get("link") or "")
    values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    return {
        "id": str(raw["id"]).strip()[:100],
        "title": str(raw.get("title") or raw["id"])[:200],
        "detail": str(raw.get("detail") or "")[:500],
        # 只放行 https 链接，前端用新窗口打开
        "link": link if link.startswith("https://") else "",
        "badges": [str(b)[:30] for b in (raw.get("badges") or [])][:5],
        "values": {
            str(k): [str(v)[:200] for v in val][:MAX_ACTION_LINES] if isinstance(val, list) else str(val)[:2000]
            for k, val in values.items()
        },
    }


def _clean_action_value(spec: ManagerAction, value: Any) -> Any:
    if spec.input == "":
        return None
    if spec.input == "text":
        if not isinstance(value, str):
            raise ValueError("请填写内容")
        if len(value) > MAX_ACTION_TEXT:
            raise ValueError(f"太长了，最多 {MAX_ACTION_TEXT} 个字")
        return value.strip()
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        raise ValueError("格式不正确")
    lines = [str(v).strip() for v in value if str(v).strip()]
    if len(lines) > MAX_ACTION_LINES:
        raise ValueError(f"最多 {MAX_ACTION_LINES} 行")
    return [line[:200] for line in lines]


@dataclass
class SettingSection:
    id: str
    title: str
    description: str = ""
    fields: list[SettingField] = field(default_factory=list)
    order: int = 100
    # 「测试连接」按钮调用的函数：成功返回说明文字，失败抛异常。
    tester: Callable[[], Awaitable[str]] | None = None
    # 配置项 → 获取可选值的函数，返回 [(值, 显示文字)]，失败抛异常。
    option_providers: dict[str, OptionsProvider] = field(default_factory=dict)
    # 文件槽（比如人设图）：WebUI 里显示上传、预览、删除。
    files: dict[str, FileSlot] = field(default_factory=dict)
    # 账号面板（比如点歌的 bot 会员账号）：WebUI 里显示登录状态、扫码登录、退出。
    accounts: AccountPanel | None = None
    # 列表管理（比如 Pixiv 关注的作者）：显示在配置项下面。
    manager: ItemManager | None = None

    def get_field(self, key: str) -> SettingField:
        for item in self.fields:
            if item.key == key:
                return item
        raise KeyError(f"{self.id} 没有配置项 {key}")


def coerce(spec: SettingField, value: Any) -> Any:
    """把输入转换成配置项需要的类型，不合法时抛出带中文说明的 ValueError。"""
    kind = spec.type
    if kind == "bool":
        if isinstance(value, bool):
            return value
        word = str(value).strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError("请填写开或关")

    if kind in {"int", "float"}:
        if isinstance(value, bool):
            raise ValueError("请填写数字")
        try:
            number = float(str(value).strip())
        except ValueError:
            raise ValueError("请填写数字") from None
        if kind == "int":
            if not number.is_integer():
                raise ValueError("请填写整数")
            number = int(number)
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"不能小于 {_fmt(spec.minimum)}")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"不能大于 {_fmt(spec.maximum)}")
        return number

    if kind == "lines":
        if isinstance(value, str):
            items: Iterable[Any] = value.splitlines()
        elif isinstance(value, list | tuple):
            items = value
        else:
            raise ValueError("格式不正确")
        return [str(item).strip() for item in items if str(item).strip()]

    if not isinstance(value, str | int | float):
        raise ValueError("格式不正确")
    text = str(value)
    if kind == "select":
        allowed = {opt[0] for opt in spec.options}
        if text not in allowed:
            raise ValueError("请从选项中选择")
        return text
    if kind != "textarea":
        text = text.strip()
    return text


_ACCOUNT_KEYS = ("platform", "name", "logged_in", "expired", "message", "account_name", "vip", "login_kinds",
                 "refreshed_at")


def _describe_accounts(panel: AccountPanel | None) -> dict[str, Any] | None:
    if panel is None:
        return None
    try:
        rows = panel.status()
    except Exception as e:  # noqa: BLE001 账号状态读不出来时不影响整个配置页
        return {"title": panel.title, "help": panel.help, "items": [], "error": str(e)[:200]}
    # 只放行白名单字段，避免哪天 status() 不小心把凭证带出来
    return {"title": panel.title, "help": panel.help,
            "items": [{k: row.get(k) for k in _ACCOUNT_KEYS} for row in rows]}


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _env_value(spec: SettingField, raw: Any) -> Any:
    """环境变量里的列表允许用逗号分隔。"""
    if spec.type == "lines" and isinstance(raw, str) and "\n" not in raw:
        raw = raw.split(",")
    return coerce(spec, raw)


class SectionView:
    """某个分区的只读视图：`view.ai_model` 每次都读取最新值。"""

    def __init__(self, store: SettingsStore, section_id: str):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_section_id", section_id)

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._store.get(self._section_id, key)
        except KeyError as e:
            raise AttributeError(str(e)) from None

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("请使用 set() 修改配置")

    def get(self, key: str) -> Any:
        return self._store.get(self._section_id, key)

    def set(self, key: str, value: Any) -> None:
        self._store.update(self._section_id, {key: value})


class SettingsStore:
    def __init__(self, path: Path, env_lookup: Callable[[str], Any] | None = None):
        self.path = Path(path)
        self._env_lookup = env_lookup or (lambda name: os.environ.get(name.upper()))
        self._sections: dict[str, SettingSection] = {}
        self._pickers: dict[str, Picker] = {}
        self._lock = threading.RLock()
        self._values: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------ 注册

    def register(self, section: SettingSection) -> SectionView:
        keys = [item.key for item in section.fields]
        if len(keys) != len(set(keys)):
            raise ValueError(f"分区 {section.id} 有重复的配置项")
        for item in section.fields:
            coerce(item, item.default)  # 默认值本身必须合法
        with self._lock:
            self._sections[section.id] = section
        return SectionView(self, section.id)

    def set_tester(self, section_id: str, tester: Callable[[], Awaitable[str]]) -> None:
        with self._lock:
            self._sections[section_id].tester = tester

    def set_account_panel(self, section_id: str, panel: AccountPanel) -> None:
        with self._lock:
            self._sections[section_id].accounts = panel

    def account_panel(self, section_id: str) -> AccountPanel:
        section = self._sections.get(section_id)
        if section is None or section.accounts is None:
            raise KeyError(f"{section_id} 没有账号面板")
        return section.accounts

    def set_manager(self, section_id: str, manager: ItemManager) -> None:
        with self._lock:
            self._sections[section_id].manager = manager

    def manager(self, section_id: str) -> ItemManager:
        section = self._sections.get(section_id)
        if section is None or section.manager is None:
            raise KeyError(f"{section_id} 没有列表")
        return section.manager

    async def manager_items(self, section_id: str) -> dict[str, Any]:
        raw = await self.manager(section_id).items()
        items = [c for c in (_clean_item(r) for r in (raw.get("items") or [])[:MAX_MANAGER_ITEMS]) if c]
        return {"items": items, "notice": str(raw.get("notice") or "")[:500]}

    async def run_manager_action(self, section_id: str, action: str, item_id: str, value: Any) -> str:
        """KeyError：没有这个列表或操作；ValueError：用户填的不对（说明直接给用户看）。"""
        manager = self.manager(section_id)
        spec = manager.action(action)
        item_id = str(item_id or "").strip()[:100]
        if spec.scope == "item" and not item_id:
            raise ValueError("没有指定是哪一项")
        return await manager.run(spec.name, item_id if spec.scope == "item" else "", _clean_action_value(spec, value))

    def add_file_slot(self, section_id: str, slot: FileSlot) -> None:
        with self._lock:
            self._sections[section_id].files[slot.key] = slot

    def file_slot(self, section_id: str, key: str) -> FileSlot:
        section = self._sections.get(section_id)
        slot = section.files.get(key) if section else None
        if slot is None:
            raise KeyError(f"{section_id}.{key} 不是文件")
        return slot

    def set_options_provider(self, section_id: str, key: str, provider: OptionsProvider) -> None:
        with self._lock:
            section = self._sections[section_id]
            if not section.get_field(key).fetchable:
                raise ValueError(f"{section_id}.{key} 没有标记为 fetchable")
            section.option_providers[key] = provider

    async def fetch_options(self, section_id: str, key: str) -> list[dict[str, str]]:
        section = self._sections.get(section_id)
        provider = section.option_providers.get(key) if section else None
        if provider is None:
            raise KeyError(f"{section_id}.{key} 不支持获取列表")
        seen: set[str] = set()
        result = []
        for value, label in await provider():
            value = str(value).strip()
            if value and value not in seen:
                seen.add(value)
                result.append({"value": value, "label": str(label or value)})
            if len(result) >= MAX_FETCHED_OPTIONS:
                break
        return result

    def add_picker(self, picker: Picker) -> None:
        with self._lock:
            section = self._sections[picker.section_id]
            for key, _ in picker.targets:
                section.get_field(key)
            self._pickers[picker.id] = picker

    def picker(self, picker_id: str) -> Picker:
        found = self._pickers.get(picker_id)
        if found is None:
            raise KeyError(f"没有 {picker_id} 这个选择页")
        return found

    async def search_picker(
        self, picker_id: str, *, query: str = "", page: int = 1, mine: bool = False, language: str = ""
    ) -> dict[str, Any]:
        raw = await self.picker(picker_id).search(query=query, page=page, mine=mine, language=language)
        items = []
        for item in (raw.get("items") or [])[:MAX_PICKER_ITEMS]:
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            sample = str(item.get("sample") or "")
            items.append(
                {
                    "value": value,
                    "title": str(item.get("title") or value),
                    "detail": str(item.get("detail") or ""),
                    # 只放行 https 地址，浏览器里直接用 <audio> 试听。
                    "sample": sample if sample.startswith("https://") else "",
                }
            )
        return {"items": items, "total": int(raw.get("total") or 0), "has_more": bool(raw.get("has_more"))}

    def describe_pickers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.id,
                "title": p.title,
                "description": p.description,
                "section_id": p.section_id,
                "targets": [{"key": key, "label": label} for key, label in p.targets],
            }
            for p in self._pickers.values()
        ]

    async def run_test(self, section_id: str) -> str:
        section = self._sections.get(section_id)
        if section is None or section.tester is None:
            raise KeyError(f"{section_id} 不支持测试连接")
        return await section.tester()

    def section(self, section_id: str) -> SectionView | None:
        return SectionView(self, section_id) if section_id in self._sections else None

    def sections(self) -> list[SettingSection]:
        return sorted(self._sections.values(), key=lambda s: (s.order, s.id))

    # ------------------------------------------------------------ 读取

    def _spec(self, section_id: str, key: str) -> SettingField:
        section = self._sections.get(section_id)
        if section is None:
            raise KeyError(f"未注册的配置分区 {section_id}")
        return section.get_field(key)

    def _from_env(self, spec: SettingField) -> tuple[bool, Any]:
        for name in spec.env:
            raw = self._env_lookup(name)
            if raw in (None, ""):
                continue
            try:
                return True, _env_value(spec, raw)
            except ValueError:
                continue
        return False, None

    def resolve(self, section_id: str, key: str) -> tuple[Any, str]:
        """返回 (值, 来源)，来源是 saved / env / default。"""
        spec = self._spec(section_id, key)
        with self._lock:
            saved = self._values.get(section_id, {})
            if key in saved:
                try:
                    return coerce(spec, saved[key]), "saved"
                except ValueError:
                    pass  # 字段定义改过、旧值不再合法时忽略
        found, value = self._from_env(spec)
        if found:
            return value, "env"
        return coerce(spec, spec.default), "default"

    def get(self, section_id: str, key: str) -> Any:
        return self.resolve(section_id, key)[0]

    # ------------------------------------------------------------ 修改

    def update(
        self,
        section_id: str,
        changes: dict[str, Any] | None = None,
        reset: Iterable[str] = (),
    ) -> dict[str, str]:
        """校验并保存。返回 {配置项: 错误说明}；有任何错误时不做任何修改。"""
        changes = changes or {}
        reset = list(reset)
        section = self._sections.get(section_id)
        if section is None:
            raise KeyError(f"未注册的配置分区 {section_id}")
        known = {item.key: item for item in section.fields}
        errors: dict[str, str] = {}
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            spec = known.get(key)
            if spec is None:
                errors[key] = "未知的配置项"
                continue
            try:
                clean[key] = coerce(spec, value)
            except ValueError as e:
                errors[key] = str(e)
        for key in reset:
            if key not in known:
                errors[key] = "未知的配置项"
        if errors:
            return errors
        with self._lock:
            saved = dict(self._values.get(section_id, {}))
            saved.update(clean)
            for key in reset:
                saved.pop(key, None)
            new_values = {**self._values, section_id: saved}
            self._save(new_values)
            self._values = new_values
        return {}

    # ------------------------------------------------------------ 给 WebUI 用

    def describe(self) -> list[dict[str, Any]]:
        result = []
        for section in self.sections():
            fields = []
            for spec in section.fields:
                value, source = self.resolve(section.id, spec.key)
                item: dict[str, Any] = {
                    "key": spec.key,
                    "label": spec.label,
                    "type": spec.type,
                    "help": spec.help,
                    "group": spec.group,
                    "placeholder": spec.placeholder,
                    "options": [{"value": v, "label": label} for v, label in spec.options],
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "source": source,
                    "env": list(spec.env),
                    "fetchable": spec.key in section.option_providers,
                }
                if spec.type == "secret":
                    # 密钥永远不发给浏览器，只告诉前端有没有设置。
                    item["value"] = ""
                    item["has_value"] = bool(value)
                else:
                    item["value"] = value
                fields.append(item)
            result.append(
                {
                    "id": section.id,
                    "title": section.title,
                    "description": section.description,
                    "testable": section.tester is not None,
                    "fields": fields,
                    "files": [
                        {
                            "key": slot.key,
                            "label": slot.label,
                            "help": slot.help,
                            "exists": slot.path().exists(),
                            "version": int(slot.path().stat().st_mtime) if slot.path().exists() else 0,
                        }
                        for slot in section.files.values()
                    ],
                    "accounts": _describe_accounts(section.accounts),
                    "manager": section.manager.describe() if section.manager else None,
                }
            )
        return result

    # ------------------------------------------------------------ 文件

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise RuntimeError(f"配置文件 {self.path} 无法读取: {e}") from e
        if not isinstance(data, dict):
            return {}
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}

    def _save(self, values: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(values, f, ensure_ascii=False, indent=2)
            with contextlib.suppress(OSError):
                Path(tmp).chmod(0o600)  # 里面有 API Key
            Path(tmp).replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise
