import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from ..storage import (
    append_chat_message,
    clear_chat_summaries,
    clear_chat_messages,
    count_chat_messages,
    get_chat_summary,
    load_chat_messages,
    set_chat_summary,
)
from ..llm.summary_service import summarize_memory


@dataclass(frozen=True)
class MemoryKey:
    scope: str
    role_id: str
    user_id: str

    def as_cache_key(self) -> str:
        return f"{self.scope}|{self.role_id}|{self.user_id}"


def build_memory_scope(event: Event | None, user_id: str) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}:user:{user_id}"
    if isinstance(event, PrivateMessageEvent):
        return f"private:{user_id}"
    return f"event:{type(event).__name__}:user:{user_id}" if event else f"user:{user_id}"


class MemoryStore:
    def load_messages(self, key: MemoryKey, limit: int) -> list[dict]:
        return load_chat_messages(key.scope, key.role_id, key.user_id, limit)

    def append_message(
        self,
        key: MemoryKey,
        message_role: str,
        content: str,
        keep_limit: int,
    ) -> None:
        append_chat_message(
            key.scope,
            key.role_id,
            key.user_id,
            message_role,
            content,
            keep_limit,
        )

    def clear_messages(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return clear_chat_messages(scope=scope, role_id=role_id, user_id=user_id)

    def clear_summaries(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return clear_chat_summaries(scope=scope, role_id=role_id, user_id=user_id)

    def count_messages(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return count_chat_messages(scope=scope, role_id=role_id, user_id=user_id)

    def get_summary(self, key: MemoryKey) -> str:
        return get_chat_summary(key.scope, key.role_id, key.user_id)

    def set_summary(self, key: MemoryKey, summary: str) -> None:
        set_chat_summary(key.scope, key.role_id, key.user_id, summary)


class NullMemoryStore:
    def load_messages(self, key: MemoryKey, limit: int) -> list[dict]:
        return []

    def append_message(
        self,
        key: MemoryKey,
        message_role: str,
        content: str,
        keep_limit: int,
    ) -> None:
        return None

    def clear_messages(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return 0

    def clear_summaries(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return 0

    def count_messages(
        self,
        scope: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
    ) -> int:
        return 0

    def get_summary(self, key: MemoryKey) -> str:
        return ""

    def set_summary(self, key: MemoryKey, summary: str) -> None:
        return None


class MemoryManager:
    def __init__(self, limit: int = 10, store=None, summary_limit: int = 600):
        self._cache = defaultdict(list)
        self._loaded_keys = set()
        self.limit = limit
        self.summary_limit = summary_limit
        self.store = store or MemoryStore()

    def make_key(
        self,
        user_id: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> MemoryKey:
        return MemoryKey(
            scope=scope or build_memory_scope(event, user_id),
            role_id=role_id or "default",
            user_id=user_id,
        )

    def get_history(
        self,
        user_id: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> list:
        key = self.make_key(user_id, role_id=role_id, event=event, scope=scope)
        self._ensure_loaded(key)
        return self._cache[key.as_cache_key()]

    def add_message(
        self,
        user_id: str,
        role: str,
        content: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> None:
        key = self.make_key(user_id, role_id=role_id, event=event, scope=scope)
        self._ensure_loaded(key)
        cache_key = key.as_cache_key()
        self._cache[cache_key].append({"role": role, "content": content})

        if len(self._cache[cache_key]) > self.limit:
            overflow = self._cache[cache_key][:-self.limit]
            self._cache[cache_key] = self._cache[cache_key][-self.limit :]
            self._merge_summary(key, overflow)

        self.store.append_message(key, role, content, self.limit)

    def get_summary(
        self,
        user_id: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> str:
        key = self.make_key(user_id, role_id=role_id, event=event, scope=scope)
        return self.store.get_summary(key)

    def set_summary(
        self,
        user_id: str,
        summary: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> None:
        key = self.make_key(user_id, role_id=role_id, event=event, scope=scope)
        self.store.set_summary(key, summary)

    def clear(
        self,
        user_id: str,
        role_id: str | None = None,
        event: Event | None = None,
        scope: str | None = None,
    ) -> int:
        if role_id is not None or event is not None or scope is not None:
            key = self.make_key(
                user_id,
                role_id=role_id or "default",
                event=event,
                scope=scope,
            )
            existed = key.as_cache_key() in self._cache
            self._cache.pop(key.as_cache_key(), None)
            self._loaded_keys.discard(key.as_cache_key())
            removed = self.store.clear_messages(
                scope=key.scope,
                role_id=key.role_id,
                user_id=key.user_id,
            )
            self.store.clear_summaries(
                scope=key.scope,
                role_id=key.role_id,
                user_id=key.user_id,
            )
            return max(1 if existed else 0, removed)

        return self.clear_user(user_id)

    def clear_user(self, user_id: str) -> int:
        keys = [key for key in self._cache if key.endswith(f"|{user_id}")]
        removed_cache = self._drop_keys(keys)
        removed_store = self.store.clear_messages(user_id=user_id)
        self.store.clear_summaries(user_id=user_id)
        return max(removed_cache, removed_store)

    def clear_scope(self, scope: str, role_id: str | None = None) -> int:
        prefix = f"{scope}|"
        keys = [
            key
            for key in self._cache
            if key.startswith(prefix)
            and (role_id is None or key.split("|", 2)[1] == role_id)
        ]
        removed_cache = self._drop_keys(keys)
        removed_store = self.store.clear_messages(scope=scope, role_id=role_id)
        self.store.clear_summaries(scope=scope, role_id=role_id)
        return max(removed_cache, removed_store)

    def clear_event_scope(self, event: Event, role_id: str | None = None) -> int:
        if isinstance(event, GroupMessageEvent):
            prefix = f"group:{event.group_id}:user:"
            keys = [
                key
                for key in self._cache
                if key.startswith(prefix)
                and (role_id is None or key.split("|", 2)[1] == role_id)
            ]
            removed_cache = self._drop_keys(keys)
            removed_store = 0
            for cache_key in keys:
                scope, key_role_id, user_id = cache_key.split("|", 2)
                removed_store += self.store.clear_messages(
                    scope=scope,
                    role_id=key_role_id,
                    user_id=user_id,
                )
                self.store.clear_summaries(
                    scope=scope,
                    role_id=key_role_id,
                    user_id=user_id,
                )
            return max(removed_cache, removed_store)

        user_id = event.get_user_id()
        return self.clear(user_id, role_id=role_id, event=event)

    def stats(self) -> dict:
        return {
            "scopes": len(self._cache),
            "messages": sum(len(messages) for messages in self._cache.values()),
            "limit": self.limit,
        }

    def scope_stats(
        self,
        user_id: str,
        role_id: str = "default",
        event: Event | None = None,
        scope: str | None = None,
    ) -> dict:
        key = self.make_key(user_id, role_id=role_id, event=event, scope=scope)
        self._ensure_loaded(key)
        summary = self.store.get_summary(key)
        return {
            "scope": key.scope,
            "role_id": key.role_id,
            "user_id": key.user_id,
            "cached_messages": len(self._cache[key.as_cache_key()]),
            "stored_messages": self.store.count_messages(
                scope=key.scope,
                role_id=key.role_id,
                user_id=key.user_id,
            ),
            "summary_chars": len(summary),
            "has_summary": bool(summary.strip()),
            "limit": self.limit,
        }

    def _drop_keys(self, keys: Iterable[str]) -> int:
        count = 0
        for key in list(keys):
            if key in self._cache:
                self._cache.pop(key, None)
                self._loaded_keys.discard(key)
                count += 1
        return count

    def _ensure_loaded(self, key: MemoryKey) -> None:
        cache_key = key.as_cache_key()
        if cache_key in self._loaded_keys:
            return
        self._cache[cache_key] = self.store.load_messages(key, self.limit)
        self._loaded_keys.add(cache_key)

    def _merge_summary(self, key: MemoryKey, messages: list[dict]) -> None:
        if not messages:
            return

        old_summary = self.store.get_summary(key).strip()
        fallback_summary = self._build_rule_summary(old_summary, messages)
        self.store.set_summary(key, fallback_summary)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(self._merge_summary_with_llm(key, old_summary, messages))

    async def _merge_summary_with_llm(
        self,
        key: MemoryKey,
        old_summary: str,
        messages: list[dict],
    ) -> None:
        summary = await summarize_memory(old_summary, messages)
        if summary:
            self.store.set_summary(key, _compact_text(summary, self.summary_limit))

    def _build_rule_summary(self, old_summary: str, messages: list[dict]) -> str:
        new_items = []
        for message in messages:
            role = "用户" if message.get("role") == "user" else "角色"
            content = _compact_text(str(message.get("content", "")), 80)
            if content:
                new_items.append(f"{role}: {content}")

        if not new_items:
            return old_summary

        merged_parts = []
        if old_summary:
            merged_parts.append(old_summary)
        merged_parts.append("较早对话摘要: " + " / ".join(new_items))

        summary = "；".join(merged_parts)
        if len(summary) > self.summary_limit:
            summary = summary[-self.summary_limit :]
            first_sep = summary.find("；")
            if first_sep != -1:
                summary = summary[first_sep + 1 :]

        return summary


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


memory_manager = MemoryManager(limit=8)
