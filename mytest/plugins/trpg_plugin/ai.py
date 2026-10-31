"""跑团的模型调用：结构化输出、重试、兜底列表、版本号。不依赖 NoneBot（发请求时才用到 httpx）。

一次调用：
1. 用当前模型请求。能用 JSON Schema 的接口传 schema；接口不认（HTTP 400）就降成 JSON 模式，再不行就只靠提示词。
   不管哪种方式，都由代码按 schema 和调用方的规则再校验一遍。
2. 无效（超时、HTTP 错误、不是 JSON、校验不过、正文为空）算一次失败，每个模型最多 3 次。
   校验不过时把错误原文给模型重来；输出被截断时下一次加大 token 上限。
3. 3 次都失败换兜底列表的下一个模型。
4. 全部失败抛 AllFailed，由流程决定怎么办（发一句场外的话，过一会儿重试）。
每次请求前后都检查版本号（still_current），版本变了就丢弃这次调用，抛 Stale。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .._shared.services.http import ServiceError, describe_exception, describe_http_error, http_client

ATTEMPTS_PER_MODEL = 3
RETRY_DELAYS = (0, 2, 5)  # 同一个模型第 1/2/3 次之前等几秒
MAX_TOKENS_CAP = 16000
MAX_ERRORS_SHOWN = 12

SCHEMA, JSON_MODE, PLAIN = "schema", "json", "plain"
MODES = (SCHEMA, JSON_MODE, PLAIN)


@dataclass(frozen=True)
class Endpoint:
    label: str  # 显示用：主模型、兜底 1
    base_url: str
    api_key: str
    model: str
    proxy: str = ""
    timeout: float = 120.0

    def key(self) -> tuple[str, str]:
        return (self.base_url.rstrip("/"), self.model)


@dataclass(frozen=True)
class Reply:
    text: str
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class Attempt:
    """一次请求的记录，写进日志、显示在运行状态页。不含密钥和提示词。"""

    label: str
    model: str
    ok: bool
    error: str = ""
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Result:
    value: Any
    endpoint: Endpoint
    attempts: list[Attempt] = field(default_factory=list)


class ModelError(Exception):
    """一次请求失败。status 是 HTTP 状态码（有的话）。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class Invalid(Exception):
    """输出不合格，errors 原样给模型看。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors


class Stale(Exception):
    """等回复的时候状态变了，这次调用作废。"""


class AllFailed(Exception):
    def __init__(self, attempts: list[Attempt]) -> None:
        last = next((a.error for a in reversed(attempts) if a.error), "没有可用的模型")
        super().__init__(f"所有模型都失败了（共 {len(attempts)} 次），最后一次：{last}")
        self.attempts = attempts


Transport = Callable[[Endpoint, dict[str, Any]], Awaitable[Reply]]


# ---------------------------------------------------------------- 配置 → 模型列表


def build_endpoints(main: dict[str, Any], role_model: str, fallbacks: list[dict[str, Any]]) -> list[Endpoint]:
    """main：「大模型」分区的 base_url / api_key / model / proxy / timeout。
    fallbacks：每项 base_url / api_key / model。base_url 留空时用主模型的地址和密钥；
    填了别的地址却没填密钥的跳过，不把主模型的密钥发给别的服务商。
    """
    proxy, timeout = str(main.get("proxy") or ""), float(main.get("timeout") or 120.0)
    main_url, main_key = str(main.get("base_url") or "").strip(), str(main.get("api_key") or "").strip()
    out: list[Endpoint] = []
    model = (role_model or "").strip() or str(main.get("model") or "").strip()
    if main_url and main_key and model:
        out.append(Endpoint("主模型", main_url, main_key, model, proxy, timeout))
    for i, fb in enumerate(fallbacks, 1):
        fb_model = str(fb.get("model") or "").strip()
        if not fb_model:
            continue
        url = str(fb.get("base_url") or "").strip()
        key = str(fb.get("api_key") or "").strip()
        if not url:
            url, key = main_url, main_key
        if not (url and key):
            continue
        out.append(Endpoint(f"兜底 {i}", url, key, fb_model, proxy, timeout))
    seen: set[tuple[str, str]] = set()
    unique = []
    for e in out:
        if e.key() not in seen:
            seen.add(e.key())
            unique.append(e)
    return unique


# ---------------------------------------------------------------- 解析和校验


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def parse_json(text: str) -> Any:
    """取出回复里的 JSON 对象：允许包在 ``` 里，或者前后带几句话。"""
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise Invalid(["输出不是一个 JSON 对象"])


def check_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """按 JSON Schema 的常用子集校验：type、enum、required、properties、additionalProperties、
    items、minItems、maxItems、minimum、maximum、minLength、maxLength。"""
    errors: list[str] = []
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind else []
    if kinds and not any(_is_type(value, k) for k in kinds):
        return [f"{path} 应该是 {'/'.join(kinds)}"]
    if "enum" in schema and value not in schema["enum"]:
        allowed = "、".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
        return [f"{path} 只能是 {allowed}"]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name} 缺少")
        for name, item in value.items():
            if name in props:
                errors += check_schema(item, props[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name} 是多余的字段")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} 至少要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 最多 {schema['maxItems']} 项")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += check_schema(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不能大于 {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} 不能为空" if schema["minLength"] == 1 else f"{path} 至少 {schema['minLength']} 字")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} 最多 {schema['maxLength']} 字")
    return errors


def _is_type(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return True


# ---------------------------------------------------------------- 发请求


async def http_transport(endpoint: Endpoint, payload: dict[str, Any]) -> Reply:
    from .._shared.services.llm import THINK_BLOCK, _text_of

    url = f"{endpoint.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {endpoint.api_key}"}
    try:
        async with http_client(endpoint.proxy, endpoint.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except ServiceError as e:
        raise ModelError(str(e)) from None
    except Exception as e:  # 网络错误：超时、连不上
        raise ModelError(describe_exception(endpoint.label, e)) from None
    if resp.status_code != 200:
        raise ModelError(describe_http_error(endpoint.label, resp), resp.status_code)
    try:
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ModelError(f"{endpoint.label} 返回格式不对：{resp.text[:200]}") from None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return Reply(
        text=THINK_BLOCK.sub("", _text_of(message.get("content"))).strip(),
        finish_reason=str(choice.get("finish_reason") or ""),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )


class ModelClient:
    def __init__(self, transport: Transport = http_transport,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, mode: str | Callable[[], str] = SCHEMA) -> None:
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self._mode = mode  # 传函数时每次调用都重新读（WebUI 里改了马上生效）
        self._modes: dict[tuple[str, str], str] = {}  # 接口不认 schema 时降级，记住

    @property
    def mode(self) -> str:
        mode = self._mode() if callable(self._mode) else self._mode
        return mode if mode in MODES else SCHEMA

    def mode_for(self, endpoint: Endpoint) -> str:
        current = self.mode
        downgraded = self._modes.get(endpoint.key())
        # 降级只会往下走；设置里选了更低的一档就以设置为准
        if downgraded is not None and MODES.index(downgraded) > MODES.index(current):
            return downgraded
        return current

    def _downgrade(self, endpoint: Endpoint) -> bool:
        current = self.mode_for(endpoint)
        if current == PLAIN:
            return False
        self._modes[endpoint.key()] = MODES[MODES.index(current) + 1]
        return True

    async def json(self, endpoints: list[Endpoint], messages: list[dict[str, str]], *, schema: dict[str, Any],
                   name: str, validate: Callable[[Any], list[str]] | None = None, max_tokens: int = 4000,
                   temperature: float = 0.3, still_current: Callable[[], bool] | None = None,
                   on_attempt: Callable[[Attempt], None] | None = None) -> Result:
        """要一个符合 schema 的 JSON 对象；validate 返回额外的错误（比如线索不在当前场景）。"""
        instruction = ("只输出一个 JSON 对象，不要输出其他文字，不要用 ``` 包起来。格式（JSON Schema）：\n"
                       + json.dumps(schema, ensure_ascii=False))
        messages = [*messages, {"role": "system", "content": instruction}]

        def parse(text: str) -> Any:
            value = parse_json(text)
            errors = check_schema(value, schema)
            if not errors and validate is not None:
                errors = list(validate(value))
            if errors:
                raise Invalid(errors)
            return value

        return await self._call(endpoints, messages, parse, max_tokens=max_tokens, temperature=temperature,
                                schema=schema, name=name, still_current=still_current, on_attempt=on_attempt)

    async def text(self, endpoints: list[Endpoint], messages: list[dict[str, str]], *,
                   validate: Callable[[str], list[str]] | None = None, max_tokens: int = 2000,
                   temperature: float = 0.8, still_current: Callable[[], bool] | None = None,
                   on_attempt: Callable[[Attempt], None] | None = None) -> Result:
        def parse(text: str) -> str:
            errors = list(validate(text)) if validate is not None else []
            if errors:
                raise Invalid(errors)
            return text

        return await self._call(endpoints, messages, parse, max_tokens=max_tokens, temperature=temperature,
                                schema=None, name="", still_current=still_current, on_attempt=on_attempt)

    async def _call(self, endpoints: list[Endpoint], messages: list[dict[str, str]], parse: Callable[[str], Any], *,
                    max_tokens: int, temperature: float, schema: dict[str, Any] | None, name: str,
                    still_current: Callable[[], bool] | None,
                    on_attempt: Callable[[Attempt], None] | None) -> Result:
        attempts: list[Attempt] = []

        def record(a: Attempt) -> None:
            attempts.append(a)
            if on_attempt is not None:
                on_attempt(a)

        def check_current() -> None:
            if still_current is not None and not still_current():
                raise Stale()

        for endpoint in endpoints:
            history = list(messages)
            tokens = max_tokens
            for n in range(ATTEMPTS_PER_MODEL):
                check_current()
                delay = RETRY_DELAYS[min(n, len(RETRY_DELAYS) - 1)]
                if delay:
                    await self.sleep(delay)
                    check_current()
                payload: dict[str, Any] = {"model": endpoint.model, "messages": history,
                                           "temperature": temperature, "max_tokens": tokens}
                mode = self.mode_for(endpoint) if schema is not None else PLAIN
                if mode == SCHEMA:
                    payload["response_format"] = {"type": "json_schema",
                                                  "json_schema": {"name": name or "output", "schema": schema}}
                elif mode == JSON_MODE:
                    payload["response_format"] = {"type": "json_object"}
                started = self.clock()
                try:
                    reply = await self.transport(endpoint, payload)
                except ModelError as e:
                    ms = int((self.clock() - started) * 1000)
                    error = str(e)
                    if e.status == 400 and "response_format" in payload and self._downgrade(endpoint):
                        error += f"（接口可能不支持这种输出格式，改用 {self.mode_for(endpoint)}）"
                    record(Attempt(endpoint.label, endpoint.model, False, error, ms))
                    continue
                ms = int((self.clock() - started) * 1000)
                check_current()
                base = dict(label=endpoint.label, model=endpoint.model, duration_ms=ms,
                            prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens)
                if reply.finish_reason == "length":
                    tokens = min(MAX_TOKENS_CAP, tokens * 2)
                    record(Attempt(ok=False, error=f"输出被截断了，下一次上限加到 {tokens}", **base))
                    continue
                if not reply.text:
                    record(Attempt(ok=False, error=f"返回了空内容（finish_reason={reply.finish_reason or '无'}）", **base))
                    continue
                try:
                    value = parse(reply.text)
                except Invalid as e:
                    shown = e.errors[:MAX_ERRORS_SHOWN]
                    record(Attempt(ok=False, error="输出不合格：" + "；".join(shown)[:300], **base))
                    history = [*history, {"role": "assistant", "content": reply.text[:4000]},
                               {"role": "user", "content": "上面的输出有这些问题，改正后重新完整输出：\n- " + "\n- ".join(shown)}]
                    continue
                record(Attempt(ok=True, **base))
                return Result(value, endpoint, attempts)
        raise AllFailed(attempts)
