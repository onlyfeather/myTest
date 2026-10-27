import sys
from pathlib import Path

import nonebot

nonebot.init(log_level="INFO")

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mytest.plugins.dice_girl_plugin.chat.memory import MemoryManager, NullMemoryStore


class FakeStore:
    def __init__(self):
        self.messages = {}
        self.summaries = {}

    def load_messages(self, key, limit: int) -> list[dict]:
        return list(self.messages.get(key.as_cache_key(), []))[-limit:]

    def append_message(self, key, message_role: str, content: str, keep_limit: int) -> None:
        cache_key = key.as_cache_key()
        self.messages.setdefault(cache_key, []).append(
            {"role": message_role, "content": content}
        )
        self.messages[cache_key] = self.messages[cache_key][-keep_limit:]

    def clear_messages(self, scope=None, role_id=None, user_id=None) -> int:
        keys = [
            key
            for key in self.messages
            if (scope is None or key.split("|", 2)[0] == scope)
            and (role_id is None or key.split("|", 2)[1] == role_id)
            and (user_id is None or key.split("|", 2)[2] == user_id)
        ]
        for key in keys:
            self.messages.pop(key, None)
        return len(keys)

    def get_summary(self, key) -> str:
        return self.summaries.get(key.as_cache_key(), "")

    def set_summary(self, key, summary: str) -> None:
        self.summaries[key.as_cache_key()] = summary


def run() -> None:
    manager = MemoryManager(limit=3, store=NullMemoryStore())

    manager.add_message("u1", "user", "private a", role_id="ra", scope="private:u1")
    manager.add_message("u1", "user", "private b", role_id="rb", scope="private:u1")
    manager.add_message("u1", "user", "group a", role_id="ra", scope="group:g1:user:u1")

    assert manager.get_history("u1", role_id="ra", scope="private:u1") == [
        {"role": "user", "content": "private a"}
    ]
    assert manager.get_history("u1", role_id="rb", scope="private:u1") == [
        {"role": "user", "content": "private b"}
    ]
    assert manager.get_history("u1", role_id="ra", scope="group:g1:user:u1") == [
        {"role": "user", "content": "group a"}
    ]

    for index in range(5):
        manager.add_message(
            "u2",
            "user",
            f"msg {index}",
            role_id="ra",
            scope="private:u2",
        )
    assert [item["content"] for item in manager.get_history("u2", "ra", scope="private:u2")] == [
        "msg 2",
        "msg 3",
        "msg 4",
    ]

    removed = manager.clear_user("u1")
    assert removed == 3
    assert manager.get_history("u1", role_id="ra", scope="private:u1") == []

    store = FakeStore()
    key_text = "private:u3|ra|u3"
    store.messages[key_text] = [{"role": "user", "content": "persisted"}]
    persisted = MemoryManager(limit=3, store=store)
    assert persisted.get_history("u3", role_id="ra", scope="private:u3") == [
        {"role": "user", "content": "persisted"}
    ]
    persisted.add_message("u3", "assistant", "saved", role_id="ra", scope="private:u3")
    assert store.messages[key_text][-1] == {"role": "assistant", "content": "saved"}
    persisted.set_summary("u3", "用户喜欢侦查。", role_id="ra", scope="private:u3")
    assert persisted.get_summary("u3", role_id="ra", scope="private:u3") == "用户喜欢侦查。"

    summary_store = FakeStore()
    summarizing = MemoryManager(limit=2, store=summary_store)
    summarizing.add_message("u4", "user", "第一条很早的消息", role_id="ra", scope="private:u4")
    summarizing.add_message("u4", "assistant", "第一条回应", role_id="ra", scope="private:u4")
    summarizing.add_message("u4", "user", "第三条消息触发裁剪", role_id="ra", scope="private:u4")
    summary = summarizing.get_summary("u4", role_id="ra", scope="private:u4")
    assert "较早对话摘要" in summary
    assert "第一条很早的消息" in summary

    run_role_switch_clears_short_term()
    print("dice girl memory tests passed")


def run_role_switch_clears_short_term() -> None:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

    class PrefixStore(FakeStore):
        def clear_messages_by_scope_prefix(self, scope_prefix: str) -> int:
            def match(key: str) -> bool:
                scope = key.split("|", 1)[0]
                if scope_prefix.endswith(":"):
                    return scope.startswith(scope_prefix)
                return scope == scope_prefix

            keys = [key for key in self.messages if match(key)]
            for key in keys:
                self.messages.pop(key)
            return len(keys)

    store = PrefixStore()
    manager = MemoryManager(limit=5, store=store)

    private = PrivateMessageEvent.model_construct(user_id=7)
    manager.add_message("7", "user", "hi a", role_id="ra", event=private)
    manager.add_message("7", "user", "hi b", role_id="rb", event=private)
    manager.set_summary("7", "长期记忆", role_id="ra", event=private)
    # 模拟重启：只在数据库里有、内存缓存里没有的记录也必须被清掉。
    store.messages["private:7|ra|7"].append({"role": "user", "content": "persisted"})
    manager._cache.clear()
    manager._loaded_keys.clear()

    manager.clear_event_scope(private)
    assert manager.get_history("7", role_id="ra", event=private) == []
    assert manager.get_history("7", role_id="rb", event=private) == []
    assert manager.get_summary("7", role_id="ra", event=private) == "长期记忆"

    group = GroupMessageEvent.model_construct(group_id=1, user_id=8)
    store.messages["group:1:user:8|ra|8"] = [{"role": "user", "content": "g"}]
    store.messages["group:10:user:8|ra|8"] = [{"role": "user", "content": "other group"}]
    manager.clear_event_scope(group)
    assert "group:1:user:8|ra|8" not in store.messages
    assert "group:10:user:8|ra|8" in store.messages


if __name__ == "__main__":
    run()
