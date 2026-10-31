"""群里的跑团指令：把一条指令变成要回复的文字。不依赖 NoneBot，handlers.py 只负责收发。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .campaign import RUNNING, Actor, CampaignError, Campaigns

COMMANDS = ("开团", "加入", "开始", "暂停", "继续", "结束", "离开", "kp")


@dataclass(frozen=True)
class Caller:
    group_id: str
    actor: Actor
    name: str  # 群名片或昵称，用自己的 .st 卡加入时当调查员名
    load_sheet: Callable[[], dict[str, int]]  # 读这个人在这个群的 .st 卡
    waiting_choices: Callable[[], list[str]] | None = None  # 还在等谁回答孤注一掷 / 花幸运
    retry: Callable[[int], None] | None = None  # /kp 重试


def run(campaigns: Campaigns, command: str, arg: str, caller: Caller) -> str:
    try:
        return _run(campaigns, command, arg.strip(), caller)
    except CampaignError as e:
        return str(e)


def _run(campaigns: Campaigns, command: str, arg: str, c: Caller) -> str:
    g, actor = c.group_id, c.actor
    if command == "开团":
        campaign, module = campaigns.open(g, arg, actor)
        lo, hi = module.players
        pregens = "、".join(p.name for p in module.pregens)
        lines = [f"「{module.title}」开始招募，需要 {lo}–{hi} 人。", f"基调：{module.tone}；时代：{module.era}。"]
        lines.append("发「/加入」用自己在这个群的 .st 角色卡加入" + (f"，或「/加入 角色名」用预设角色：{pregens}。" if pregens else "。"))
        lines.append("人齐了由开团的人发「/开始」。")
        return "\n".join(lines)

    if command == "加入":
        if arg:
            player = campaigns.join(g, actor, pregen=arg)
        else:
            player = campaigns.join(g, actor, own_sheet=c.load_sheet(), own_name=c.name)
        campaign = campaigns.active(g)
        title = f"{player.occupation}「{player.name}」" if player.occupation else f"「{player.name}」"
        if campaign.phase == RUNNING or player.status != "present":
            return f"{title}会在下一轮登场。"
        return f"{title}加入了。现在 {len(campaigns.players(campaign.id, ('present', 'joining')))} 人。"

    if command == "离开":
        player, paused = campaigns.leave(g, actor)
        campaign = campaigns.active(g)
        if paused:
            return f"「{player.name}」离开了。团里没有调查员了，先暂停，有人「/加入」后发「/继续」。"
        if campaign.phase == "recruiting" or player.status == "joining":
            return f"「{player.name}」退出了。"
        return f"「{player.name}」会在下一轮离开。"

    if command == "开始":
        campaign, module = campaigns.start(g, actor)
        names = "、".join(p.name for p in campaigns.players(campaign.id, ("present",)))
        scene = module.scene(campaign.scene)
        opening = f"{module.intro}\n\n【{scene.name}】{scene.public}"
        campaigns.store.add_log(campaign.id, 0, "narration", {"text": opening}, campaign.updated_at, visibility="public")
        return (f"「{module.title}」开始。调查员：{names}。\n\n{opening}\n\n"
                "直接说你的调查员要做什么；括号里的话是场外，（KP …）是问 KP 的场外问题。")

    if command == "暂停":
        campaigns.pause(g, actor)
        return "团暂停了。发「/继续」接着玩。"

    if command == "继续":
        campaigns.resume(g, actor)
        return "继续。"

    if command == "结束":
        campaign = campaigns.end(g, actor, arg)
        return f"「{campaign.module_title}」结束了。"

    if command == "kp":
        if arg in ("", "状态"):
            text = campaigns.status_text(g)
            if c.waiting_choices:
                names = c.waiting_choices()
                if names:
                    text += "\n等待回答孤注一掷 / 花幸运：" + "、".join(names)
            return text
        if arg == "重试":
            campaign = campaigns.active(g)
            if campaign is None:
                return "这个群现在没有团。"
            if not (actor.is_manager or actor.user_id == campaign.opener):
                return "只有开团的人或群管理员能重试。"
            if c.retry is not None:
                c.retry(campaign.id)
            return "好，马上重试。"
        return "可用：/kp 状态、/kp 重试"

    return ""
