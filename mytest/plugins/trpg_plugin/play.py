"""收齐一轮之后怎么办。不依赖 NoneBot。

结算（理解行动、裁决、掷骰、叙述）还没做，现在先用代码拼一段确认，直接进入下一轮，
方便在群里把收集、场外、超时、中途加入和离开的流程走通。
"""

from __future__ import annotations

from .campaign import Campaigns
from .rounds import Rounds


def settle_placeholder(campaigns: Campaigns, rounds: Rounds, campaign_id: int, round_no: int, now: int) -> str:
    campaign = campaigns.get(campaign_id)
    state = rounds.current(campaign)
    if campaign is None or state is None or state.no != round_no:
        return ""
    names = {p.user_id: p.name for p in campaigns.players(campaign_id)}
    actions = rounds.merged_actions(campaign_id, round_no)
    lines = [f"第 {round_no} 轮收齐了："]
    for user_id, text in actions.items():
        lines.append(f"· {names.get(user_id, user_id)}：{text}")
    for user_id in state.standby:
        lines.append(f"· {names.get(user_id, user_id)}：原地待命")
    lines.append("（KP 的结算还在开发中，这一轮先不处理，直接进入下一轮。）")

    change = rounds.advance(campaign_id, now)
    for p in change.entered:
        lines.append(f"「{p.name}」加入了调查。")
    for p in change.exited:
        lines.append(f"「{p.name}」离开了。")
    return "\n".join(lines)
