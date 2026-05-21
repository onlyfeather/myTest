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

    manager.add_message("u1", "user", "private ling", role_id="ling", scope="private:u1")
    manager.add_message("u1", "user", "private shizuku", role_id="shizuku", scope="private:u1")
    manager.add_message("u1", "user", "group ling", role_id="ling", scope="group:g1:user:u1")

    assert manager.get_history("u1", role_id="ling", scope="private:u1") == [
        {"role": "user", "content": "private ling"}
    ]
    assert manager.get_history("u1", role_id="shizuku", scope="private:u1") == [
        {"role": "user", "content": "private shizuku"}
    ]
    assert manager.get_history("u1", role_id="ling", scope="group:g1:user:u1") == [
        {"role": "user", "content": "group ling"}
    ]

    for index in range(5):
        manager.add_message(
            "u2",
            "user",
            f"msg {index}",
            role_id="ling",
            scope="private:u2",
        )
    assert [item["content"] for item in manager.get_history("u2", "ling", scope="private:u2")] == [
        "msg 2",
        "msg 3",
        "msg 4",
    ]

    removed = manager.clear_user("u1")
    assert removed == 3
    assert manager.get_history("u1", role_id="ling", scope="private:u1") == []

    store = FakeStore()
    key_text = "private:u3|ling|u3"
    store.messages[key_text] = [{"role": "user", "content": "persisted"}]
    persisted = MemoryManager(limit=3, store=store)
    assert persisted.get_history("u3", role_id="ling", scope="private:u3") == [
        {"role": "user", "content": "persisted"}
    ]
    persisted.add_message("u3", "assistant", "saved", role_id="ling", scope="private:u3")
    assert store.messages[key_text][-1] == {"role": "assistant", "content": "saved"}
    persisted.set_summary("u3", "用户喜欢侦查。", role_id="ling", scope="private:u3")
    assert persisted.get_summary("u3", role_id="ling", scope="private:u3") == "用户喜欢侦查。"

    summary_store = FakeStore()
    summarizing = MemoryManager(limit=2, store=summary_store)
    summarizing.add_message("u4", "user", "第一条很早的消息", role_id="ling", scope="private:u4")
    summarizing.add_message("u4", "assistant", "第一条回应", role_id="ling", scope="private:u4")
    summarizing.add_message("u4", "user", "第三条消息触发裁剪", role_id="ling", scope="private:u4")
    summary = summarizing.get_summary("u4", role_id="ling", scope="private:u4")
    assert "较早对话摘要" in summary
    assert "第一条很早的消息" in summary

    print("dice girl memory tests passed")


if __name__ == "__main__":
    run()
