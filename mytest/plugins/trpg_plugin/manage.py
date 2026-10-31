"""WebUI 里的两张列表：跑团模组、进行中的团。不依赖 NoneBot（由 handlers 注册到 settings_store）。

模组：列出自带和上传的模组，上传 JSON（校验不过列出每个问题）、看校验结果、删除上传的。
进行中的团：KP 视角状态（含秘密，只给 WebUI 的管理员看）、最近日志、马上重试、揭示 / 收回线索、改理智和幸运、结束。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from .campaign import PHASE_NAMES, Campaigns
from .module import load_module_text
from .play import Engine

MAX_LOG_LINES = 30


class ModuleManager:
    def __init__(self, campaigns: Campaigns, clock: Callable[[], float] = time.time) -> None:
        self.campaigns = campaigns
        self.library = campaigns.library
        self.clock = clock

    async def items(self) -> dict:
        items = []
        for m, source, builtin in self.library.all():
            result = load_module_text(source)
            warn = len(result.warnings)
            detail = f"{m.players[0]}–{m.players[1]} 人 · {len(m.scenes)} 个场景 · {len(m.clues)} 条线索 · {m.tone}"
            if warn:
                detail += f" · {warn} 个提醒"
            items.append({"id": m.id, "title": m.title, "detail": detail,
                          "badges": ["自带"] if builtin else ["上传"]})
        bad = [r for r in self.library.store.modules() if r["id"] not in {i["id"] for i in items}]
        for r in bad:
            items.append({"id": r["id"], "title": r["title"], "detail": "校验不过，不能用来开团", "badges": ["有错误"]})
        return {"items": items, "notice": "" if items else "还没有模组"}

    async def run(self, action: str, item_id: str, value) -> str:
        if action == "upload":
            result = self.library.upload(str(value or ""), int(self.clock()))
            if not result.ok:
                raise ValueError("没有导入。" + result.report())
            note = f"已导入「{result.module.title}」"
            return note + (f"\n{result.report()}" if result.warnings else "")
        if action == "check":
            source = self._source(item_id)
            return load_module_text(source).report()
        if action == "delete":
            if any(m.id == item_id and builtin for m, _, builtin in self.library.all()):
                raise ValueError("自带的模组不能删除")
            if any(c.module_id == item_id for c in self.campaigns.unfinished()):
                raise ValueError("有团正在用这个模组，结束之后才能删除")
            if not self.library.delete(item_id):
                raise ValueError("没有这个模组")
            return "已删除"
        raise ValueError("没有这个操作")

    def _source(self, module_id: str) -> str:
        for m, source, _ in self.library.all():
            if m.id == module_id:
                return source
        row = next((r for r in self.library.store.modules() if r["id"] == module_id), None)
        if row is None:
            raise ValueError("没有这个模组")
        return row["source"]


class CampaignManager:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.campaigns = engine.campaigns
        self.store = engine.store

    async def items(self) -> dict:
        items = []
        for c in self.campaigns.unfinished():
            m = self.campaigns.module(c)
            scene = m.scene(c.scene)
            state = self.engine.rounds.current(c) if c.round else None
            stage = {"collecting": "收集行动", "collected": "等结算", "resolved": "等叙述", "done": "已完成"}
            players = self.campaigns.players(c.id, ("present", "joining", "leaving"))
            detail = f"群 {c.group_id} · 第 {c.round} 轮" + (f" · {scene.name}" if scene else "")
            if state is not None:
                detail += f" · {stage.get(state.phase, state.phase)}"
            waiting = self.engine.waiting_choices(c.id)
            if waiting:
                detail += f" · 等 {'、'.join(waiting)} 回答"
            detail += " · " + ("、".join(p.name for p in players) or "没有调查员")
            items.append({"id": str(c.id), "title": f"「{c.module_title}」", "detail": detail,
                          "badges": [PHASE_NAMES[c.phase]],
                          "values": {"edit": [f"{p.name} 理智{p.san} 幸运{p.luck}" for p in players]}})
        return {"items": items, "notice": "" if items else "现在没有进行中的团"}

    async def run(self, action: str, item_id: str, value) -> str:
        c = self._campaign(item_id)
        if action == "state":
            return self.kp_view(c.id)
        if action == "log":
            return self.recent_log(c.id)
        if action == "retry":
            self.engine.retry_now(c.id)
            return "下一次检查（30 秒内）会马上重试"
        if action == "reveal":
            return self._reveal(c, str(value or "").strip(), reveal=True)
        if action == "hide":
            return self._reveal(c, str(value or "").strip(), reveal=False)
        if action == "edit":
            return self._edit(c, value or [])
        if action == "end":
            self.campaigns.finish(c.id, "在 WebUI 里结束")
            self.engine.outbox_put(c.group_id, [f"「{c.module_title}」结束了。"])
            return "已结束"
        raise ValueError("没有这个操作")

    def _campaign(self, item_id: str):
        try:
            c = self.campaigns.get(int(item_id))
        except ValueError:
            c = None
        if c is None or c.phase == "ended":
            raise ValueError("这个团已经结束了，刷新一下列表")
        return c

    def kp_view(self, campaign_id: int) -> str:
        c = self.campaigns.get(campaign_id)
        m = self.campaigns.module(c)
        flags = self.store.flags(campaign_id)
        revealed = {r["clue"]: r for r in self.store.clues(campaign_id)}
        names = {p.user_id: p.name for p in self.campaigns.players(campaign_id)}
        lines = [f"「{m.title}」第 {c.round} 轮 · {PHASE_NAMES[c.phase]} · 版本 {c.revision}",
                 f"当前场景：{c.scene} {m.scene(c.scene).name if m.scene(c.scene) else ''}", "", "调查员："]
        for p in self.campaigns.players(campaign_id):
            lines.append(f"  {p.name}（{p.user_id}）{p.status} 理智{p.san} 幸运{p.luck}" + (f" HP{p.hp}" if p.hp else ""))
        lines += ["", "线索："]
        for clue in m.clues:
            r = revealed.get(clue.id)
            mark = f"已揭示（第 {r['round']} 轮，{names.get(r['user_id'], r['user_id'])}）" if r else "未揭示"
            lines.append(f"  {clue.id}{'【关键】' if clue.key else ''} {clue.where}：{mark}")
        lines += ["", "标记："]
        lines += [f"  {f.id} {f.desc}：{'已设置' if f.id in flags else '未设置'}" for f in m.flags]
        npcs = {r["npc"]: r for r in self.store.npcs(campaign_id)}
        lines += ["", "NPC："]
        for n in m.npcs:
            r = npcs.get(n.id)
            if r is not None:
                lines.append(f"  {n.name}：{r['attitude']} · 在 {r['location'] or '未知'}"
                             f" · {'见过' if r['appeared'] else '没见过'} · {'秘密已说出' if r['secret_told'] else '秘密未说'}")
        fired = self.store.fired_events(campaign_id)
        lines += ["", "事件：" + ("、".join(sorted(fired)) or "还没有")]
        summary = self.store.latest_summary(campaign_id)
        if summary:
            lines += ["", f"摘要（到第 {summary['upto_round']} 轮）：", summary["text"]]
        lines += ["", "真相：", m.truth]
        return "\n".join(lines)

    def recent_log(self, campaign_id: int) -> str:
        rows = self.store.logs(campaign_id, limit=MAX_LOG_LINES)
        lines = []
        for r in reversed(rows):
            data = json.loads(r["data"])
            when = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
            if r["kind"] == "model":
                state = "成功" if data.get("ok") else f"失败：{data.get('error', '')[:80]}"
                lines.append(f"{when} 第{r['round_no']}轮 {data.get('stage')} · {data.get('label')} {data.get('model')} · "
                             f"{data.get('duration_ms')}ms · {state}")
            elif r["kind"] == "narration":
                flag = "（安全版本）" if data.get("safe") else ""
                problems = "；".join(data.get("problems") or [])
                lines.append(f"{when} 第{r['round_no']}轮 叙述{flag}：{data.get('text', '')[:60]}…"
                             + (f" 没通过检查：{problems[:120]}" if problems else ""))
            else:
                lines.append(f"{when} 第{r['round_no']}轮 {r['kind']}：{json.dumps(data, ensure_ascii=False)[:120]}")
        return "\n".join(lines) or "还没有记录"

    def _reveal(self, c, clue_id: str, *, reveal: bool) -> str:
        m = self.campaigns.module(c)
        clue = m.clue(clue_id)
        if clue is None:
            raise ValueError(f"没有线索 {clue_id}。可选：{'、'.join(x.id for x in m.clues)}")
        now = int(time.time())
        with self.store.transaction() as db:
            if reveal:
                if not self.store.reveal_clue(db, c.id, clue_id, "", c.round):
                    raise ValueError("这条线索已经揭示过了")
                for f in clue.set_flags:
                    self.store.set_flag(db, c.id, f, c.round)
            else:
                if db.execute("DELETE FROM campaign_clue WHERE campaign_id = ? AND clue = ?", (c.id, clue_id)).rowcount == 0:
                    raise ValueError("这条线索还没有揭示")
            self.store.bump_revision(db, c.id, now)
        return f"已{'揭示' if reveal else '收回'} {clue_id}（不会在群里说，下一轮 KP 按新状态处理）"

    def _edit(self, c, lines) -> str:
        """每行「调查员名 理智50 幸运40」。"""
        import re

        players = {p.name: p for p in self.campaigns.players(c.id)}
        changes = []
        now = int(time.time())
        with self.store.transaction() as db:
            for line in lines:
                m = re.match(r"^\s*(\S+)\s+(.*)$", str(line))
                if not m or m.group(1) not in players:
                    raise ValueError(f"「{line}」：开头要写调查员名，可选 {'、'.join(players)}")
                p = players[m.group(1)]
                fields = {}
                for key, number in re.findall(r"(理智|幸运|HP|体力)\s*(\d+)", m.group(2)):
                    fields[{"理智": "san", "幸运": "luck", "HP": "hp", "体力": "hp"}[key]] = min(99, int(number))
                if not fields:
                    raise ValueError(f"「{line}」：没有可以改的数值，写成「{p.name} 理智50 幸运40」")
                changed = {k: v for k, v in fields.items() if getattr(p, k) != v}
                if changed:
                    self.store.update_player(db, c.id, p.user_id, **changed)
                    changes.append(f"{p.name} " + " ".join(f"{k}={v}" for k, v in changed.items()))
            if changes:
                self.store.bump_revision(db, c.id, now)
        return "已修改：" + "；".join(changes) if changes else "没有变化"
