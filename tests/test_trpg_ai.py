"""跑团的模型调用层：结构化输出、重试、兜底、版本号。运行：python -m pytest tests/test_trpg_ai.py -q"""

import asyncio
import json

import httpx
import pytest

from mytest.plugins.trpg_plugin import ai
from mytest.plugins.trpg_plugin.ai import (
    AllFailed,
    Endpoint,
    Invalid,
    ModelClient,
    ModelError,
    Reply,
    Stale,
    build_endpoints,
    check_schema,
    parse_json,
)

SCHEMA = {
    "type": "object",
    "required": ["skill", "difficulty"],
    "additionalProperties": False,
    "properties": {
        "skill": {"type": "string", "minLength": 1},
        "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"]},
        "bonus": {"type": "integer", "minimum": -2, "maximum": 2},
        "clues": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
}
MAIN = Endpoint("主模型", "https://a/v1", "ka", "m1")
FB = Endpoint("兜底 1", "https://b/v1", "kb", "m2")
GOOD = '{"skill": "侦查", "difficulty": "hard"}'


class Script:
    """按顺序返回准备好的结果（Reply、字符串或异常），记下每次请求。"""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list[tuple[Endpoint, dict]] = []

    async def __call__(self, endpoint, payload):
        self.calls.append((endpoint, json.loads(json.dumps(payload))))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Reply) else Reply(item)


async def no_sleep(_):
    return None


def client(script, **kw):
    return ModelClient(script, sleep=no_sleep, **kw)


def ask(c, endpoints=(MAIN, FB), **kw):
    return asyncio.run(c.json(list(endpoints), [{"role": "user", "content": "裁决"}], schema=SCHEMA, name="arbiter", **kw))


# ---------------------------------------------------------------- 解析和校验


def test_parse_json_variants():
    assert parse_json(GOOD) == {"skill": "侦查", "difficulty": "hard"}
    assert parse_json(f"```json\n{GOOD}\n```")["skill"] == "侦查"
    assert parse_json(f"好的，结果如下：\n{GOOD}\n希望有帮助")["difficulty"] == "hard"
    for bad in ("", "不是 JSON", "{坏掉的"):
        with pytest.raises(Invalid):
            parse_json(bad)


def test_check_schema():
    assert check_schema({"skill": "侦查", "difficulty": "hard", "bonus": 1, "clues": ["c1"]}, SCHEMA) == []
    errors = check_schema({"skill": "", "difficulty": "很难", "bonus": 3, "clues": ["a", "b", "c", 4], "x": 1}, SCHEMA)
    joined = "\n".join(errors)
    for part in ("$.skill 不能为空", "$.difficulty 只能是", "$.bonus 不能大于 2", "$.clues 最多 3 项",
                 "$.clues[3] 应该是 string", "$.x 是多余的字段"):
        assert part in joined, joined
    assert check_schema({"skill": "侦查"}, SCHEMA) == ["$.difficulty 缺少"]
    assert check_schema([], SCHEMA) == ["$ 应该是 object"]
    assert check_schema({"skill": "a", "difficulty": "hard", "bonus": True}, SCHEMA) == ["$.bonus 应该是 integer"]


# ---------------------------------------------------------------- 重试和兜底


def test_first_try():
    script = Script(GOOD)
    result = ask(client(script))
    assert result.value == {"skill": "侦查", "difficulty": "hard"} and result.endpoint == MAIN
    assert [a.ok for a in result.attempts] == [True]
    payload = script.calls[0][1]
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "JSON Schema" in payload["messages"][-1]["content"]


def test_invalid_output_is_sent_back_with_errors():
    script = Script('{"skill": "侦查", "difficulty": "很难"}', GOOD)
    result = ask(client(script))
    assert result.value["difficulty"] == "hard" and len(result.attempts) == 2
    retry = script.calls[1][1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"skill": "侦查", "difficulty": "很难"}'}
    assert "$.difficulty 只能是" in retry[-1]["content"]


def test_custom_validation():
    script = Script(GOOD, '{"skill": "侦查", "difficulty": "regular"}')

    def only_regular(value):
        return [] if value["difficulty"] == "regular" else ["线索 c3 不在当前场景"]

    result = ask(client(script), validate=only_regular)
    assert result.value["difficulty"] == "regular"
    assert "线索 c3 不在当前场景" in result.attempts[0].error


def test_falls_back_after_three_failures():
    script = Script(ModelError("超时"), "不是 JSON", Reply("", "stop"), GOOD)
    result = ask(client(script))
    assert result.endpoint == FB
    assert [(a.label, a.ok) for a in result.attempts] == [("主模型", False)] * 3 + [("兜底 1", True)]
    assert "空内容" in result.attempts[2].error
    assert script.calls[3][0] == FB
    assert len(script.calls[3][1]["messages"]) == 2, "换模型时不带上一个模型的错误对话"


def test_all_failed():
    script = Script(*[ModelError(f"错{i}") for i in range(6)])
    with pytest.raises(AllFailed) as info:
        ask(client(script))
    assert len(info.value.attempts) == 6 and "错5" in str(info.value)
    with pytest.raises(AllFailed, match="没有可用的模型"):
        ask(client(Script()), endpoints=())


def test_truncated_output_gets_more_tokens():
    script = Script(Reply('{"skill": "侦', "length"), GOOD)
    result = ask(client(script), max_tokens=3000)
    assert result.value["skill"] == "侦查"
    assert [c[1]["max_tokens"] for c in script.calls] == [3000, 6000]
    assert "截断" in result.attempts[0].error


def test_retry_waits_between_attempts():
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    script = Script(ModelError("x"), ModelError("y"), GOOD)
    ask(ModelClient(script, sleep=sleep))
    assert waits == [2, 5]


# ---------------------------------------------------------------- 输出格式降级


def test_schema_unsupported_downgrades_and_is_remembered():
    script = Script(ModelError("HTTP 400 response_format", 400), ModelError("HTTP 400", 400), GOOD, GOOD)
    c = client(script)
    ask(c)
    kinds = [call[1].get("response_format", {}).get("type") for call in script.calls]
    assert kinds == ["json_schema", "json_object", None]
    ask(c)
    assert "response_format" not in script.calls[3][1], "同一个接口记住降级"
    assert c.mode_for(FB) == "schema", "别的接口不受影响"


def test_other_http_errors_do_not_downgrade():
    script = Script(ModelError("HTTP 401", 401), GOOD)
    c = client(script)
    ask(c)
    assert c.mode_for(MAIN) == "schema"


def test_mode_setting():
    script = Script(GOOD)
    ask(client(script, mode="json"))
    assert script.calls[0][1]["response_format"] == {"type": "json_object"}
    script = Script(GOOD)
    ask(client(script, mode="plain"))
    assert "response_format" not in script.calls[0][1]


# ---------------------------------------------------------------- 版本号


def test_stale_before_request():
    with pytest.raises(Stale):
        ask(client(Script(GOOD)), still_current=lambda: False)


def test_stale_when_reply_arrives():
    state = {"rev": 1}
    script = Script(GOOD)

    async def transport(endpoint, payload):
        state["rev"] = 2  # 等回复的时候有人撤回了行动
        return await script(endpoint, payload)

    c = ModelClient(transport, sleep=no_sleep)
    with pytest.raises(Stale):
        ask(c, still_current=lambda: state["rev"] == 1)


# ---------------------------------------------------------------- 纯文本


def test_text_with_validation():
    script = Script("里面有暗格", "老周叹了口气")

    def no_spoiler(text):
        return ["出现了没揭示的内容：暗格"] if "暗格" in text else []

    result = asyncio.run(client(script).text([MAIN], [{"role": "user", "content": "叙述"}], validate=no_spoiler))
    assert result.value == "老周叹了口气"
    assert "response_format" not in script.calls[0][1]


def test_attempt_callback():
    seen = []
    ask(client(Script(ModelError("x"), GOOD)), on_attempt=seen.append)
    assert [a.ok for a in seen] == [False, True]


# ---------------------------------------------------------------- 配置


def test_build_endpoints():
    main = {"base_url": "https://a/v1", "api_key": "ka", "model": "m0", "proxy": "", "timeout": 60}
    fallbacks = [
        {"base_url": "", "api_key": "", "model": "m-same-provider"},
        {"base_url": "https://b/v1", "api_key": "kb", "model": "m-other"},
        {"base_url": "https://c/v1", "api_key": "", "model": "m-no-key"},
        {"base_url": "", "api_key": "", "model": ""},
    ]
    eps = build_endpoints(main, "m-arbiter", fallbacks)
    assert [(e.label, e.base_url, e.api_key, e.model) for e in eps] == [
        ("主模型", "https://a/v1", "ka", "m-arbiter"),
        ("兜底 1", "https://a/v1", "ka", "m-same-provider"),
        ("兜底 2", "https://b/v1", "kb", "m-other"),
    ], "别的服务商没填密钥的跳过，不发主模型的密钥"
    assert build_endpoints(main, "", [])[0].model == "m0"
    assert build_endpoints(main, "m0", [{"base_url": "", "api_key": "", "model": "m0"}]) == [eps[0].__class__(
        "主模型", "https://a/v1", "ka", "m0", "", 60.0)], "重复的去掉"
    no_main_key = build_endpoints({**main, "api_key": ""}, "", fallbacks)
    assert [e.label for e in no_main_key] == ["兜底 2"], "主密钥没填时，同服务商的兜底也跳过"


# ---------------------------------------------------------------- HTTP


def test_http_transport(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.headers["authorization"] != "Bearer ka":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        body = {"choices": [{"message": {"content": "<think>想想</think>好"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        return httpx.Response(200, json=body)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_client(proxy="", timeout=60.0):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    monkeypatch.setattr(ai, "http_client", fake_client)
    reply = asyncio.run(ai.http_transport(MAIN, {"model": "m1", "messages": []}))
    assert (reply.text, reply.finish_reason, reply.prompt_tokens, reply.completion_tokens) == ("好", "stop", 10, 2)
    assert str(calls[0].url) == "https://a/v1/chat/completions"
    with pytest.raises(ModelError) as info:
        asyncio.run(ai.http_transport(Endpoint("兜底 1", "https://b/v1", "wrong", "m"), {}))
    assert info.value.status == 401 and "bad key" in str(info.value)
