"""一起听小程序（微信 / QQ）技术验证用的服务端。只用于测试，不接入 bot，测完可以删掉。

提供测试音频、一个 WebSocket（每 5 秒发一次 tick，按脚本发远程控制指令），
并把小程序发回来的日志连同收到的时间一起打印出来、写进 probe-log.jsonl。

运行：python server.py --port 8091
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import time
import wave
from array import array
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

LOG_FILE = Path(__file__).with_name("probe-log.jsonl")
RATE = 8000
TICK_SECONDS = 5

# 测试二的远程指令：(开始后第几秒, 动作, 参数)。开始后有 15 秒时间锁屏。
REMOTE_SCRIPT: list[tuple[int, str, dict[str, Any]]] = [
    (25, "pause", {}),
    (35, "play", {}),
    (45, "seek", {"position": 5}),
    (60, "src", {"n": 2, "s": 120}),
    (80, "pause", {}),
]

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def now_ms() -> int:
    return int(time.time() * 1000)


def record(client: str, source: str, text: str, client_ms: int | None = None) -> None:
    """打印一行日志。client_ms 是小程序记下这条日志的时间：和收到时间差很多，说明这条消息在手机上积压过。"""
    received = now_ms()
    stamp = datetime.fromtimestamp(received / 1000).strftime("%H:%M:%S.%f")[:-3]
    delay = ""
    if client_ms is not None:
        delay = f"  (手机 {datetime.fromtimestamp(client_ms / 1000).strftime('%H:%M:%S')}，晚到 {received - client_ms} ms)"
    print(f"{stamp} [{client}] {source}: {text}{delay}", flush=True)  # noqa: T201 测试脚本直接打印
    entry = {"received_ms": received, "client": client, "source": source, "text": text, "client_ms": client_ms}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@lru_cache(maxsize=16)
def test_tone(n: int, seconds: int) -> bytes:
    """第 n 首：一个低音持续响着，每 3 秒响 n 声高音，听声音就知道在放第几首。"""
    base = (262, 330, 392, 440)[(n - 1) % 4]
    samples = array("h")
    for i in range(RATE * seconds):
        t = i / RATE
        cycle = t % 3
        beep = any(k * 0.25 <= cycle < k * 0.25 + 0.12 for k in range(n))
        if beep:
            samples.append(int(9000 * math.sin(2 * math.pi * 1000 * t)))
        else:
            samples.append(int(2500 * math.sin(2 * math.pi * base * t)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


@app.get("/audio/{n}.wav")
async def audio(n: int, s: int = 15) -> Response:
    n = max(1, min(n, 9))
    s = max(5, min(s, 300))
    return Response(test_tone(n, s), media_type="audio/wav", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/next")
async def next_track(n: int = 1) -> JSONResponse:
    """测试一 B 用：小程序在一首放完后先请求这里，再换歌，用来看后台能不能联网。"""
    return JSONResponse({"ok": True, "n": n, "server_ms": now_ms()})


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    client = (socket.query_params.get("c") or "?")[:16]
    record(client, "服务端", "WebSocket 连上了")
    lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()

    async def send(message: dict[str, Any]) -> None:
        async with lock:
            await socket.send_json(message)

    async def ticker() -> None:
        seq = 0
        while True:
            await asyncio.sleep(TICK_SECONDS)
            seq += 1
            await send({"type": "tick", "seq": seq, "server_ms": now_ms()})

    async def remote_script() -> None:
        record(client, "服务端", "测试二开始：按脚本发远程指令")
        started = time.monotonic()
        for index, (at, action, args) in enumerate(REMOTE_SCRIPT, 1):
            await asyncio.sleep(max(0.0, at - (time.monotonic() - started)))
            await send({"type": "cmd", "id": index, "action": action, **args})
            record(client, "服务端", f"发出指令 #{index} {action} {args or ''}")
        record(client, "服务端", "测试二的指令都发完了")

    def spawn(coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    spawn(ticker())
    try:
        while True:
            raw = await socket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "log":
                record(client, "手机", str(msg.get("msg", ""))[:500], msg.get("t"))
            elif kind == "ack":
                state = {k: msg.get(k) for k in ("currentTime", "paused", "src")}
                record(client, "手机", f"指令 #{msg.get('id')} {msg.get('action')} 执行后：{state}", msg.get("t"))
            elif kind == "remote_test":
                spawn(remote_script())
    except WebSocketDisconnect as e:
        record(client, "服务端", f"WebSocket 断开 code={e.code}")
    finally:
        for task in list(tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="一起听小程序技术验证服务端")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 在容器里给隧道访问
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    print(f"日志同时写在 {LOG_FILE}", flush=True)  # noqa: T201
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
