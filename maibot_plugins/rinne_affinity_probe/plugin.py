"""临时探测插件：记录几个回复相关 Hook 实际收到的参数结构。

只读不写：所有处理器都原样放行，自身出错也只记日志，不会影响麦麦回复。
记录写到插件数据目录下的 probe.jsonl，每个 Hook 最多记录 MAX_RECORDS_PER_HOOK 次。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode

MAX_RECORDS_PER_HOOK = 15
MAX_STR = 300
MAX_KEYS = 40
MAX_DEPTH = 4


def summarize(value: Any, depth: int = 0) -> Any:
    """把任意参数压缩成便于阅读的结构：保留字段名和类型，长文本截断，长列表只取第一项。"""
    if depth >= MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"...<共{len(value)}字>"
    if isinstance(value, dict):
        keys = list(value.keys())
        result = {str(k): summarize(value[k], depth + 1) for k in keys[:MAX_KEYS]}
        if len(keys) > MAX_KEYS:
            result["<省略>"] = f"还有 {len(keys) - MAX_KEYS} 个字段"
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return {
            "<类型>": type(value).__name__,
            "<长度>": len(items),
            "<第一项>": summarize(items[0], depth + 1) if items else None,
            "<最后一项>": summarize(items[-1], depth + 1) if len(items) > 1 else None,
        }
    for attr in ("model_dump", "dict"):
        dump = getattr(value, attr, None)
        if callable(dump):
            try:
                return {"<对象>": type(value).__name__, **summarize(dump(), depth + 1)}
            except Exception:
                break
    text = repr(value)
    return f"<{type(value).__name__}> " + (text if len(text) <= MAX_STR else text[:MAX_STR] + "...")


class RinneAffinityProbe(MaiBotPlugin):
    async def on_load(self) -> None:
        self._counts: dict[str, int] = {}
        self._file: Path | None = None
        try:
            data_dir = Path(self.ctx.paths.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            self._file = data_dir / "probe.jsonl"
        except Exception as e:
            self.ctx.logger.warning("[探测] 拿不到插件数据目录，只写日志: %s", e)

        try:
            import importlib.metadata as metadata

            sdk_version = metadata.version("maibot-plugin-sdk")
        except Exception:
            sdk_version = "未知"
        self._record("_plugin_loaded", {"sdk_version": sdk_version, "file": str(self._file)})
        self.ctx.logger.info("[探测] 已加载，SDK %s，记录文件 %s", sdk_version, self._file)

    async def on_unload(self) -> None:
        self.ctx.logger.info("[探测] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        return None

    def _record(self, hook: str, kwargs: dict[str, Any]) -> None:
        count = self._counts.get(hook, 0)
        if count >= MAX_RECORDS_PER_HOOK:
            return
        self._counts[hook] = count + 1
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hook": hook,
            "index": count + 1,
            "keys": sorted(str(k) for k in kwargs),
            "kwargs": summarize(kwargs),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        self.ctx.logger.info("[探测] %s #%d 参数字段: %s", hook, count + 1, entry["keys"])
        if self._file is not None:
            try:
                with self._file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                self.ctx.logger.warning("[探测] 写记录文件失败: %s", e)

    async def _try_lookup_reply_target(self, kwargs: dict[str, Any]) -> None:
        """顺便试一下：能不能用 reply_message_id 查到被回复的那条消息（也就是在回复谁）。"""
        message_id = kwargs.get("reply_message_id")
        stream_id = kwargs.get("session_id") or kwargs.get("stream_id") or ""
        if not message_id:
            return
        try:
            found = await self.ctx.message.get_by_id(str(message_id), stream_id=str(stream_id))
            self._record("_lookup_reply_target_ok", {"message_id": message_id, "result": found})
        except Exception as e:
            self._record(
                "_lookup_reply_target_failed",
                {"message_id": message_id, "error": f"{type(e).__name__}: {e}"},
            )

    @HookHandler(
        "maisaka.replyer.before_request",
        name="probe_replyer_before_request",
        mode=HookMode.BLOCKING,
        error_policy=ErrorPolicy.SKIP,
    )
    async def probe_replyer_before_request(self, **kwargs: Any) -> dict[str, Any]:
        try:
            self._record("maisaka.replyer.before_request", kwargs)
            await self._try_lookup_reply_target(kwargs)
        except Exception as e:
            self.ctx.logger.warning("[探测] before_request 记录失败: %s", e)
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="probe_replyer_after_response",
        mode=HookMode.BLOCKING,
        error_policy=ErrorPolicy.SKIP,
    )
    async def probe_replyer_after_response(self, **kwargs: Any) -> dict[str, Any]:
        try:
            self._record("maisaka.replyer.after_response", kwargs)
        except Exception as e:
            self.ctx.logger.warning("[探测] after_response 记录失败: %s", e)
        return {"action": "continue"}

    @HookHandler(
        "chat.receive.after_process",
        name="probe_receive_after_process",
        mode=HookMode.BLOCKING,
        error_policy=ErrorPolicy.SKIP,
    )
    async def probe_receive_after_process(self, **kwargs: Any) -> dict[str, Any]:
        try:
            self._record("chat.receive.after_process", kwargs)
        except Exception as e:
            self.ctx.logger.warning("[探测] receive 记录失败: %s", e)
        return {"action": "continue"}

    @HookHandler(
        "send_service.after_send",
        name="probe_after_send",
        mode=HookMode.OBSERVE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def probe_after_send(self, **kwargs: Any) -> None:
        try:
            self._record("send_service.after_send", kwargs)
        except Exception as e:
            self.ctx.logger.warning("[探测] after_send 记录失败: %s", e)


def create_plugin():
    return RinneAffinityProbe()
