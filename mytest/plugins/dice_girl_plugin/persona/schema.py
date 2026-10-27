"""角色卡的场景化读取。

角色卡结构向后兼容：没有新字段时，私聊和群聊都按原来的写法工作，
只是群聊会用更短的默认回复长度。

新增的可选字段：

    "meta": {"aliases": ["凛音", "小凛"]}
    "duty": {"on": {...}, "off": {...}, "off_duty_min_favorability": 86,
             "night_off_duty_min_favorability": 60, "night_hours": [21, 6]}
    "stages": [{"user_call": "主人", "off_duty_call": "..."}]
    "off_duty_examples": [...]
    "modes": {
        "private": {"reply_max_chars": 180, "actions": true, "rules": [...]},
        "group":   {"reply_max_chars": 60,  "actions": false, "rules": [...],
                    "stance": "她在群里的定位"}
    }
    "private_only": {
        "behavior_habits": [...],
        "emotional_triggers": {...},
        "intimacy_boundaries": [...]
    }

`private_only` 里的内容只在私聊注入；群聊里只有当世界书或用户输入明确提到时，
才由世界书词条补充进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PRIVATE = "private"
GROUP = "group"

_DEFAULT_MODES = {
    PRIVATE: {"reply_max_chars": 180, "actions": True, "rules": [], "stance": ""},
    GROUP: {
        "reply_max_chars": 60,
        "actions": False,
        "rules": [
            "像群里的普通成员一样说话，一般一两句话，不写大段动作描写。",
            "只回应和你有关、或者在叫你的那个人，不要逐个点评群里所有人。",
            "不要复述别人刚说过的话，不要总结群聊。",
        ],
        "stance": "",
    },
}


@dataclass
class SceneProfile:
    scene: str
    reply_max_chars: int
    actions: bool
    rules: list[str] = field(default_factory=list)
    stance: str = ""


def role_name(role: dict) -> str:
    return role.get("meta", {}).get("name") or role.get("name") or "骰娘"


def role_aliases(role: dict) -> list[str]:
    """角色的名字和所有别名，去重并保持顺序。"""
    names = [role_name(role), *role.get("meta", {}).get("aliases", [])]
    seen: set[str] = set()
    result = []
    for name in names:
        clean = str(name).strip()
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            result.append(clean)
    return result


def scene_profile(role: dict, scene: str) -> SceneProfile:
    base = dict(_DEFAULT_MODES[scene])
    custom = role.get("modes", {}).get(scene, {})
    base.update({k: v for k, v in custom.items() if v is not None})
    return SceneProfile(
        scene=scene,
        reply_max_chars=int(base["reply_max_chars"]),
        actions=bool(base["actions"]),
        rules=list(base.get("rules") or []),
        stance=str(base.get("stance") or ""),
    )


@dataclass
class DutyState:
    off_duty: bool
    label: str
    description: str
    rules: list[str] = field(default_factory=list)


# 判断上下班使用北京时间，避免云服务器的 UTC 时区让“晚上”提前或推迟 8 小时。
BEIJING = timezone(timedelta(hours=8))


def _in_hours(hour: int, span: list[int]) -> bool:
    start, end = span
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def duty_state(role: dict, scene: str, favorability: int, now: datetime | None = None) -> DutyState | None:
    """角色有 duty 配置时返回当前是上班还是下班；没有配置返回 None。

    规则：群聊永远上班；私聊里好感度达到 off_duty_min_favorability 时下班，
    夜间时段（night_hours，北京时间）门槛降到 night_off_duty_min_favorability。
    """
    duty = role.get("duty")
    if not duty:
        return None

    off = False
    if scene == PRIVATE:
        hour = (now or datetime.now(BEIJING)).astimezone(BEIJING).hour
        threshold = int(duty.get("off_duty_min_favorability", 101))
        if _in_hours(hour, duty.get("night_hours", [21, 6])):
            threshold = min(threshold, int(duty.get("night_off_duty_min_favorability", threshold)))
        off = favorability >= threshold

    info = duty.get("off" if off else "on", {})
    return DutyState(
        off_duty=off,
        label=str(info.get("label") or ("下班后" if off else "工作中")),
        description=str(info.get("description") or ""),
        rules=list(info.get("rules") or []),
    )


def examples_for(role: dict, state: DutyState | None) -> list:
    if state and state.off_duty and role.get("off_duty_examples"):
        return role["off_duty_examples"]
    return role.get("examples", [])


def user_call_for(stage: dict, state: DutyState | None, default: str) -> str:
    if state and state.off_duty and stage.get("off_duty_call"):
        return str(stage["off_duty_call"])
    return str(stage.get("user_call") or default)


def persona_for_scene(role: dict, scene: str) -> dict:
    """返回某个场景下生效的 persona：私聊时把 private_only 合并进去。"""
    persona = dict(role.get("persona", {}))
    if scene != PRIVATE:
        return persona

    extra = role.get("private_only", {})
    for key in ("behavior_habits", "intimacy_boundaries", "speech_constraints"):
        if extra.get(key):
            persona[key] = [*persona.get(key, []), *extra[key]]
    if extra.get("emotional_triggers"):
        persona["emotional_triggers"] = {
            **persona.get("emotional_triggers", {}),
            **extra["emotional_triggers"],
        }
    return persona
