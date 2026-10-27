import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["DICE_GIRL_DATA_DIR"] = tempfile.mkdtemp(prefix="dicegirl_mem_test_")
os.environ["DICE_GIRL_AI_API_KEY"] = "test"
os.environ["DICE_GIRL_EMBEDDING_API_KEY"] = "test"
os.environ["DICE_GIRL_DIGEST_EVERY_MESSAGES"] = "10"

import nonebot

nonebot.init(log_level="WARNING")

from mytest.plugins.dice_girl_plugin.group import memory as gm
from mytest.plugins.dice_girl_plugin.group.memory_score import freshness, recall_score, retention_score
from mytest.plugins.dice_girl_plugin.llm import embedding
from mytest.plugins.dice_girl_plugin.storage import group_repository as repo
from mytest.plugins.dice_girl_plugin.storage.group_repository import GroupEvent, GroupMessage

DAY = 86400


def event(importance=3, created=0.0, recalled=None, count=0, keywords=(), participants=()):
    return GroupEvent(
        id=1, group_id="g", content="x", participants=list(participants), keywords=list(keywords),
        importance=importance, created_at=created, last_recalled_at=recalled, recall_count=count,
    )


def test_scoring():
    now = 100 * DAY
    old_minor = event(importance=1, created=now - 30 * DAY)
    old_major = event(importance=5, created=now - 30 * DAY)
    assert freshness(old_major, now) > freshness(old_minor, now)
    assert retention_score(old_major, now) > retention_score(old_minor, now)
    # 被想起过会重新计时
    revived = event(importance=1, created=now - 30 * DAY, recalled=now - DAY, count=2)
    assert retention_score(revived, now) > retention_score(old_minor, now)
    # 有相似度时语义优先
    relevant = recall_score(event(), now=now, similarity=0.9, keyword=0.0)
    irrelevant = recall_score(event(importance=5), now=now, similarity=0.1, keyword=0.0)
    assert relevant > irrelevant


def test_vector_roundtrip_and_cosine():
    vec = [0.1, -0.5, 2.0]
    back = repo.unpack_vector(repo.pack_vector(vec))
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, back))
    assert abs(embedding.cosine([1, 0], [1, 0]) - 1) < 1e-9
    assert embedding.cosine([1, 0], [0, 1]) == 0
    assert embedding.cosine([1, 0], [1, 0, 0]) == 0


def test_parse_digest_is_lenient():
    summary, events, lore = gm.parse_digest({
        "group_status": "最近在聊团",
        "events": [
            {"content": "小明和小红约好周六跑团", "participants": ["小明", "小红"], "keywords": ["周六", "跑团", "x"], "importance": 9},
            {"content": "短"},
            "bad",
        ],
        "new_lore": [{"keys": ["拉莱耶", "a"], "content": "群里管迟到叫拉莱耶时间"}, {"keys": [], "content": "无效"}],
    })
    assert summary == "最近在聊团"
    assert len(events) == 1 and events[0]["importance"] == 5 and events[0]["keywords"] == ["周六", "跑团"]
    assert lore == [{"keys": ["拉莱耶"], "content": "群里管迟到叫拉莱耶时间"}]


def _add_messages(group_id, count, start_time, keep=1000, retention=10**9):
    for i in range(count):
        repo.append_group_message(
            group_id,
            GroupMessage(user_id="u1", display_name="小明", content=f"消息{i} 周六跑团", is_bot=False, created_at=start_time + i),
            keep_limit=keep,
            retention_seconds=retention,
        )


def test_retention_waits_for_digest():
    old = time.time() - 10 * DAY
    _add_messages("keep", 5, old, retention=DAY)
    # 新消息到来会触发清理；旧消息还没整理过，所以不能删
    _add_messages("keep", 1, time.time(), retention=DAY)
    assert len(repo.load_group_messages("keep", 100)) == 6
    # 整理到最后一条旧消息之后，旧消息才能按时间清理
    last_old = repo.load_group_messages("keep", 100)[4].id
    assert repo.save_digest_and_events("keep", "s", last_old, 0, [], time.time())
    _add_messages("keep", 1, time.time(), retention=DAY)
    assert len(repo.load_group_messages("keep", 100)) == 2


def test_digest_position_guard():
    assert repo.save_digest_and_events("guard", "a", 10, 0, [], 1.0)
    # 位置已经被推进到 10，基于旧位置 0 的第二次整理必须放弃
    assert not repo.save_digest_and_events("guard", "b", 20, 0, [{"content": "重复"}], 2.0)
    assert repo.get_group_digest("guard").summary == "a"
    assert repo.list_group_events("guard") == []


def test_archive_keeps_valuable_events():
    now = time.time()
    events = [
        {"content": f"小事{i}", "importance": 1} for i in range(4)
    ] + [{"content": "大事", "importance": 5}]
    repo.save_digest_and_events("arch", "", 1, 0, events, now - 60 * DAY)
    assert repo.archive_stale_events("arch", keep=2) == 3
    remaining = [e.content for e in repo.list_group_events("arch")]
    assert "大事" in remaining and len(remaining) == 2


def test_digest_and_recall_end_to_end():
    calls = {"llm": 0, "embed": 0}

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def fake_llm(client, payload, prefix):
        calls["llm"] += 1
        content = json.dumps({
            "group_status": "大家在约周六的跑团",
            "events": [{"content": "小明约大家周六晚上跑克苏鲁团", "participants": ["小明"], "keywords": ["周六", "跑团", "克苏鲁"], "importance": 4}],
            "new_lore": [{"keys": ["拉莱耶时间"], "content": "群里把迟到叫拉莱耶时间"}],
        }, ensure_ascii=False)
        return Resp({"choices": [{"message": {"content": content}}]})

    def fake_vector(text):
        return [1.0, 0.0] if ("跑团" in text or "团" in text) else [0.0, 1.0]

    class FakeEmbedClient:
        is_closed = False

        async def post(self, url, headers, json):
            calls["embed"] += 1
            data = [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(json["input"])]
            return Resp({"data": data})

    gm.post_chat_completion = fake_llm
    embedding._client = FakeEmbedClient()

    group_id = "e2e"
    _add_messages(group_id, 12, time.time() - 100)
    assert gm.should_digest(group_id)

    async def run():
        assert await gm.digest_group(group_id, "白鸟凛音")
        assert not gm.should_digest(group_id)
        events = repo.list_group_events(group_id)
        assert len(events) == 1 and events[0].embedding == [1.0, 0.0]
        assert repo.get_group_digest(group_id).summary == "大家在约周六的跑团"
        lore = repo.list_group_lore(group_id)
        assert lore and lore[0].created_by == gm.AUTO_LORE_AUTHOR
        # 同一个梗不会重复写入
        _add_messages(group_id, 12, time.time())
        await gm.digest_group(group_id, "白鸟凛音")
        assert len(repo.list_group_lore(group_id)) == 1

        # 语义命中：没有共同关键词，但向量相近
        hit = await gm.recall(group_id, "这周末的团还开吗", [])
        assert hit and "克苏鲁" in hit[0].content
        assert repo.list_group_events(group_id)[0].recall_count >= 1
        # 不相关的话题不会被硬塞进来
        assert await gm.recall(group_id, "今天吃什么", []) == []

        text = gm.format_memory(repo.get_group_digest(group_id), hit)
        assert "近期群况" in text and "今天" in text

    asyncio.run(run())
    assert calls["llm"] == 2 and calls["embed"] >= 3


def test_recall_without_embedding_uses_keywords():
    os.environ.pop("DICE_GIRL_EMBEDDING_API_KEY", None)
    from mytest.plugins.dice_girl_plugin.config import config

    saved_key = config.embedding_api_key
    config.embedding_api_key = ""
    try:
        repo.save_digest_and_events("kw", "", 1, 0, [{"content": "小红养了一只橘猫", "keywords": ["橘猫"], "participants": ["小红"], "importance": 3}], time.time())

        async def run():
            assert await gm.recall("kw", "你家橘猫还好吗", [])
            assert await gm.recall("kw", "随便聊聊", ["小红"])
            assert await gm.recall("kw", "随便聊聊", []) == []

        asyncio.run(run())
    finally:
        config.embedding_api_key = saved_key


def run() -> None:
    for name, func in list(globals().items()):
        if name.startswith("test_"):
            func()
    print("dice girl group memory tests passed")


if __name__ == "__main__":
    run()
