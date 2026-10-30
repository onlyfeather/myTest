"""团的状态机：开团、加入、开始、暂停、继续、结束、离开。不依赖 NoneBot。

阶段：recruiting（招募）→ running（进行中）⇄ paused（暂停）→ ended（结束）。
调查员状态：
- present：在场。招募时加入的直接在场。
- joining：开团后中途加入，下一轮开始时编一个理由让他登场，变成 present。
- leaving：开团后离开，下一轮开始时编一个退场理由，变成 left。
- left：已离开。

出错时抛 CampaignError，消息直接发给群里。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .module import BUILTIN_DIR, LoadResult, Module, Problem, load_module_text
from .rules.skills import canonical
from .store import Store

RECRUITING, RUNNING, PAUSED, ENDED = "recruiting", "running", "paused", "ended"
PHASE_NAMES = {RECRUITING: "招募中", RUNNING: "进行中", PAUSED: "暂停中", ENDED: "已结束"}

JOINING, PRESENT, LEAVING, LEFT = "joining", "present", "leaving", "left"
ACTIVE = (JOINING, PRESENT)

MAX_NAME = 12
REQUIRED_SHEET = ("理智", "智力", "幸运")


class CampaignError(Exception):
    """操作不成立。消息直接显示给用户。"""


@dataclass(frozen=True)
class Actor:
    """谁在操作。is_manager：群管理员、群主或 superuser，由指令那边判断。"""

    user_id: str
    is_manager: bool = False


@dataclass(frozen=True)
class Campaign:
    id: int
    group_id: str
    module_id: str
    module_title: str
    phase: str
    opener: str
    scene: str
    round: int
    game_time: str
    revision: int
    end_reason: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Player:
    user_id: str
    name: str
    occupation: str
    pregen: str
    sheet: dict[str, int]
    san: int
    luck: int
    hp: int | None
    items: tuple[str, ...]
    status: str
    joined_round: int
    left_round: int | None


@dataclass(frozen=True)
class MembershipChange:
    """一轮开始时的登场和退场，叙述时给他们编一句理由。"""

    entered: tuple[Player, ...]
    exited: tuple[Player, ...]


def _campaign(row) -> Campaign:
    return Campaign(**{k: row[k] for k in Campaign.__dataclass_fields__})


def _player(row) -> Player:
    return Player(
        user_id=row["user_id"], name=row["name"], occupation=row["occupation"], pregen=row["pregen"],
        sheet=json.loads(row["sheet"]), san=row["san"], luck=row["luck"], hp=row["hp"],
        items=tuple(json.loads(row["items"])), status=row["status"],
        joined_round=row["joined_round"], left_round=row["left_round"],
    )


def normalize_sheet(sheet: dict[str, int]) -> dict[str, int]:
    """技能名统一写法，数值限制在 0..99。"""
    out: dict[str, int] = {}
    for name, value in sheet.items():
        out[canonical(str(name))] = max(0, min(99, int(value)))
    return out


def derived_hp(sheet: dict[str, int]) -> int | None:
    if "体质" in sheet and "体型" in sheet:
        return (sheet["体质"] + sheet["体型"]) // 10
    return sheet.get("体力")


# ---------------------------------------------------------------- 模组库


class ModuleLibrary:
    """插件自带的模组（文件）加上 WebUI 上传的模组（数据库）。"""

    def __init__(self, store: Store, builtin_dir: Path = BUILTIN_DIR):
        self.store = store
        self.builtin_dir = builtin_dir

    def _builtin(self) -> dict[str, str]:
        out = {}
        for path in sorted(self.builtin_dir.glob("*.json")):
            source = path.read_text(encoding="utf-8")
            result = load_module_text(source)
            if result.ok:
                out[result.module.id] = source
        return out

    def all(self) -> list[tuple[Module, str, bool]]:
        """[(模组, 原文, 是不是自带的)]；上传的模组和自带的同名时以自带的为准。"""
        items = [(load_module_text(s).module, s, True) for s in self._builtin().values()]
        builtin_ids = {m.id for m, _, _ in items}
        for row in self.store.modules():
            if row["id"] in builtin_ids:
                continue
            result = load_module_text(row["source"])
            if result.ok:
                items.append((result.module, row["source"], False))
        return items

    def find(self, name: str) -> tuple[Module, str]:
        name = name.strip()
        modules = self.all()
        for module, source, _ in modules:
            if name in (module.id, module.title):
                return module, source
        titles = "、".join(f"「{m.title}」" for m, _, _ in modules) or "（还没有模组）"
        if not name:
            raise CampaignError(f"要说模组名，例如：/开团 {modules[0][0].title if modules else '茶馆失物'}。现有模组：{titles}")
        raise CampaignError(f"没有叫「{name}」的模组。现有模组：{titles}")

    def upload(self, source: str, now: int) -> LoadResult:
        result = load_module_text(source)
        if result.ok:
            if result.module.id in self._builtin():
                result.problems.append(Problem("error", "id", f"编号 {result.module.id} 和自带的模组重复了，换一个 id"))
                result.module = None
                return result
            self.store.save_module(result.module.id, result.module.title, source, now)
        return result

    def delete(self, module_id: str) -> bool:
        return self.store.delete_module(module_id)


# ---------------------------------------------------------------- 团


class Campaigns:
    def __init__(self, store: Store, library: ModuleLibrary, clock: Callable[[], float] = time.time):
        self.store = store
        self.library = library
        self.clock = clock
        self._modules: dict[int, Module] = {}

    def _now(self) -> int:
        return int(self.clock())

    # ---- 读取

    def active(self, group_id: str) -> Campaign | None:
        row = self.store.active_campaign(group_id)
        return _campaign(row) if row else None

    def get(self, campaign_id: int) -> Campaign | None:
        row = self.store.campaign(campaign_id)
        return _campaign(row) if row else None

    def unfinished(self) -> list[Campaign]:
        return [_campaign(r) for r in self.store.unfinished_campaigns()]

    def players(self, campaign_id: int, statuses: tuple[str, ...] | None = None) -> list[Player]:
        players = [_player(r) for r in self.store.players(campaign_id)]
        return [p for p in players if statuses is None or p.status in statuses]

    def player(self, campaign_id: int, user_id: str) -> Player | None:
        row = self.store.player(campaign_id, user_id)
        return _player(row) if row else None

    def module(self, campaign: Campaign) -> Module:
        """开团时的模组快照。"""
        if campaign.id not in self._modules:
            result = load_module_text(self.store.module_source(campaign.id))
            if not result.ok:
                raise CampaignError(f"这个团的模组读不出来了：{result.report()}")
            self._modules[campaign.id] = result.module
        return self._modules[campaign.id]

    def _require(self, group_id: str) -> Campaign:
        campaign = self.active(group_id)
        if campaign is None:
            raise CampaignError("这个群现在没有团。发「/开团 模组名」开一个")
        return campaign

    @staticmethod
    def _require_control(campaign: Campaign, actor: Actor, what: str) -> None:
        if not (actor.is_manager or actor.user_id == campaign.opener):
            raise CampaignError(f"只有开团的人或群管理员能{what}")

    # ---- 操作

    def open(self, group_id: str, module_name: str, actor: Actor) -> tuple[Campaign, Module]:
        existing = self.active(group_id)
        if existing is not None:
            raise CampaignError(f"这个群已经有一个{PHASE_NAMES[existing.phase]}的团「{existing.module_title}」，结束之后才能开新的")
        module, source = self.library.find(module_name)
        try:
            cid = self.store.insert_campaign(group_id, module.id, module.title, source, actor.user_id, self._now())
        except Exception as e:  # 同时有两个人开团，唯一索引挡住了第二个
            if "UNIQUE" in str(e):
                raise CampaignError("这个群刚刚已经开了一个团") from e
            raise
        self._modules[cid] = module
        return self.get(cid), module

    def join(self, group_id: str, actor: Actor, *, pregen: str = "", own_sheet: dict[str, int] | None = None,
             own_name: str = "") -> Player:
        """pregen 不为空时用模组的预设角色，否则用自己的 .st 卡（own_sheet）。"""
        campaign = self._require(group_id)
        module = self.module(campaign)
        existing = self.player(campaign.id, actor.user_id)
        if existing and existing.status in ACTIVE:
            raise CampaignError(f"你已经在团里了，调查员是「{existing.name}」")
        if existing and existing.status == LEAVING:
            raise CampaignError("你刚离开，等下一轮你的调查员退场之后才能重新加入")
        active = self.players(campaign.id, ACTIVE)
        if len(active) >= module.players[1]:
            raise CampaignError(f"这个模组最多 {module.players[1]} 人，已经满了")

        if pregen:
            chosen = next((p for p in module.pregens if p.name == pregen.strip()), None)
            if chosen is None:
                names = "、".join(p.name for p in module.pregens) or "（这个模组没有预设角色）"
                raise CampaignError(f"没有叫「{pregen.strip()}」的预设角色。可选：{names}")
            taken = next((p for p in active if p.pregen == chosen.name), None)
            if taken is not None:
                raise CampaignError(f"「{chosen.name}」已经有人用了")
            name, occupation, sheet = chosen.name, chosen.occupation, normalize_sheet(chosen.sheet)
        else:
            sheet = normalize_sheet(own_sheet or {})
            if not sheet:
                names = "、".join(p.name for p in module.pregens)
                hint = f"，或者用预设角色：/加入 {module.pregens[0].name}（可选：{names}）" if names else ""
                raise CampaignError(f"你在这个群还没有角色卡。先用 .st 录一张{hint}")
            missing = [n for n in REQUIRED_SHEET if n not in sheet]
            if missing:
                raise CampaignError(f"你的角色卡缺少 {'、'.join(missing)}，用 .st 补上再加入")
            name = (own_name.strip() or actor.user_id)[:MAX_NAME]
            occupation = ""
        if any(p.name == name for p in active):
            raise CampaignError(f"团里已经有叫「{name}」的调查员了，改个群名片或换一个角色再加入")

        status = PRESENT if campaign.phase == RECRUITING else JOINING
        with self.store.transaction() as db:
            self.store.upsert_player(
                db, campaign.id, actor.user_id, name=name, occupation=occupation, pregen=pregen.strip(),
                sheet=sheet, san=sheet["理智"], luck=sheet["幸运"], hp=derived_hp(sheet), status=status,
                joined_round=campaign.round, now=self._now(),
            )
            self.store.bump_revision(db, campaign.id, self._now())
        return self.player(campaign.id, actor.user_id)

    def leave(self, group_id: str, actor: Actor) -> tuple[Player, bool]:
        """返回（离开的调查员，团是否因此暂停）。招募中直接删掉；开团后等下一轮退场。"""
        campaign = self._require(group_id)
        player = self.player(campaign.id, actor.user_id)
        if player is None or player.status not in ACTIVE:
            raise CampaignError("你不在这个团里")
        now = self._now()
        paused = False
        with self.store.transaction() as db:
            if campaign.phase == RECRUITING or player.status == JOINING:
                # 还没在故事里出现过，直接去掉
                self.store.delete_player(db, campaign.id, actor.user_id)
            else:
                self.store.update_player(db, campaign.id, actor.user_id, status=LEAVING)
            remaining = [p for p in self.players(campaign.id, ACTIVE) if p.user_id != actor.user_id]
            if campaign.phase == RUNNING and not remaining:
                self.store.update_campaign(db, campaign.id, now, phase=PAUSED)
                paused = True
            else:
                self.store.bump_revision(db, campaign.id, now)
        return player, paused

    def start(self, group_id: str, actor: Actor) -> tuple[Campaign, Module]:
        campaign = self._require(group_id)
        if campaign.phase != RECRUITING:
            raise CampaignError(f"团已经开始了（{PHASE_NAMES[campaign.phase]}）")
        self._require_control(campaign, actor, "开始")
        module = self.module(campaign)
        count = len(self.players(campaign.id, ACTIVE))
        if count < module.players[0]:
            raise CampaignError(f"这个模组至少要 {module.players[0]} 人，现在只有 {count} 人。发「/加入」加入")
        now = self._now()
        with self.store.transaction() as db:
            for npc in module.npcs:
                where = next((s.id for s in module.scenes if npc.id in s.npcs), "")
                self.store.insert_npc(db, campaign.id, npc.id, npc.attitude, where)
            self.store.update_campaign(db, campaign.id, now, phase=RUNNING, scene=module.start_scene, round=1)
        return self.get(campaign.id), module

    def pause(self, group_id: str, actor: Actor) -> Campaign:
        campaign = self._require(group_id)
        self._require_control(campaign, actor, "暂停")
        if campaign.phase != RUNNING:
            raise CampaignError(f"团现在{PHASE_NAMES[campaign.phase]}，不能暂停")
        with self.store.transaction() as db:
            self.store.update_campaign(db, campaign.id, self._now(), phase=PAUSED)
        return self.get(campaign.id)

    def resume(self, group_id: str, actor: Actor) -> Campaign:
        campaign = self._require(group_id)
        self._require_control(campaign, actor, "继续")
        if campaign.phase != PAUSED:
            raise CampaignError(f"团现在{PHASE_NAMES[campaign.phase]}，不用继续")
        if not self.players(campaign.id, ACTIVE):
            raise CampaignError("团里没有调查员了。先「/加入」再继续")
        with self.store.transaction() as db:
            self.store.update_campaign(db, campaign.id, self._now(), phase=RUNNING)
        return self.get(campaign.id)

    def end(self, group_id: str, actor: Actor, reason: str = "") -> Campaign:
        campaign = self._require(group_id)
        self._require_control(campaign, actor, "结束团")
        return self.finish(campaign.id, reason or "手动结束")

    def finish(self, campaign_id: int, reason: str) -> Campaign:
        """结束一个团（手动结束或者达成结局）。"""
        with self.store.transaction() as db:
            self.store.update_campaign(db, campaign_id, self._now(), phase=ENDED, end_reason=reason[:100])
        self._modules.pop(campaign_id, None)
        return self.get(campaign_id)

    def settle_membership(self, campaign_id: int) -> MembershipChange:
        """新一轮开始时调用：中途加入的人登场，离开的人退场。"""
        campaign = self.get(campaign_id)
        players = self.players(campaign_id)
        entered = tuple(p for p in players if p.status == JOINING)
        exited = tuple(p for p in players if p.status == LEAVING)
        if entered or exited:
            with self.store.transaction() as db:
                for p in entered:
                    self.store.update_player(db, campaign_id, p.user_id, status=PRESENT)
                for p in exited:
                    self.store.update_player(db, campaign_id, p.user_id, status=LEFT, left_round=campaign.round)
                self.store.bump_revision(db, campaign_id, self._now())
        return MembershipChange(entered, exited)

    # ---- 显示

    def status_text(self, group_id: str) -> str:
        """/kp 状态。只放玩家能看到的东西。"""
        campaign = self.active(group_id)
        if campaign is None:
            return "这个群现在没有团。发「/开团 模组名」开一个"
        module = self.module(campaign)
        lines = [f"「{campaign.module_title}」{PHASE_NAMES[campaign.phase]}"]
        if campaign.phase != RECRUITING:
            scene = module.scene(campaign.scene)
            lines.append(f"第 {campaign.round} 轮，在{scene.name if scene else '未知的地方'}")
        players = self.players(campaign.id, (PRESENT, JOINING, LEAVING))
        if players:
            notes = {JOINING: "（下一轮登场）", LEAVING: "（下一轮退场）"}
            lines.append("调查员：" + "、".join(f"{p.name}{notes.get(p.status, '')}" for p in players))
        else:
            lines.append("还没有调查员")
        if campaign.phase == RECRUITING:
            lo, hi = module.players
            lines.append(f"需要 {lo}–{hi} 人。发「/加入」用自己的 .st 卡，或「/加入 角色名」用预设角色："
                         + "、".join(p.name for p in module.pregens if p.name not in {x.pregen for x in players}))
        return "\n".join(lines)
