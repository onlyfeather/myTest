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

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"未知的配置类型: {self.type}")


@dataclass
class SettingSection:
    id: str
    title: str
    description: str = ""
    fields: list[SettingField] = field(default_factory=list)
    order: int = 100
    # 「测试连接」按钮调用的函数：成功返回说明文字，失败抛异常。
    tester: Callable[[], Awaitable[str]] | None = None

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
