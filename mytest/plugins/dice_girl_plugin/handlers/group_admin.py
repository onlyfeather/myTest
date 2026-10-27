"""群聊相关管理命令。

群梗：所有群员可以添加；删除限添加者本人、群管理员、群主和 SUPERUSER。
昵称：可以给自己加 / 删；给别人改需要群管理员以上。
档案：所有人能看自己的；忘记某人需要群管理员以上（本人也可以让她忘记自己）。
群开关：群管理员以上。
"""

import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from ..group import members, profiles
from ..group import memory as group_memory
from ..llm.embedding import embedding_enabled
from ..persona import persona_manager
from ..storage import get_user_favorability
from ..storage import group_repository as repo

LORE_HELP = (
    "群梗用法：\n"
    "/dice 群梗 添加 关键词1,关键词2 内容\n"
    "/dice 群梗 列表\n"
    "/dice 群梗 删除 编号\n"
    "关键词至少 2 个字；群里提到这些词时她会知道是什么意思，也可能接一句话。"
)


async def _is_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    return (
        await SUPERUSER(bot, event)
        or await GROUP_ADMIN(bot, event)
        or await GROUP_OWNER(bot, event)
    )


def _at_target(args: Message) -> str | None:
    for segment in args:
        if segment.type == "at" and segment.data.get("qq") not in (None, "all"):
            return str(segment.data["qq"])
    return None


lore_cmd = on_command("dice 群梗", priority=5, block=True)
alias_cmd = on_command("dice 昵称", priority=5, block=True)
profile_cmd = on_command("dice 档案", priority=5, block=True)
forget_cmd = on_command("dice 忘记", priority=5, block=True)
group_switch_cmd = on_command("dice 群设置", priority=5, block=True)
memory_cmd = on_command("dice 群记忆", priority=5, block=True)
reload_cmd = on_command("dice 重载", permission=SUPERUSER, priority=5, block=True)


@lore_cmd.handle()
async def handle_lore(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = str(event.group_id)
    parts = args.extract_plain_text().strip().split(maxsplit=2)
    action = parts[0] if parts else ""

    if action == "添加" and len(parts) == 3:
        keys = [k.strip() for k in parts[1].replace("，", ",").split(",") if len(k.strip()) >= 2]
        if not keys:
            await lore_cmd.finish("关键词至少要 2 个字。\n" + LORE_HELP)
        lore_id = repo.add_group_lore(group_id, keys, parts[2].strip(), event.get_user_id())
        await lore_cmd.finish(f"✅ 已记下群梗 #{lore_id}：{'、'.join(keys)}")

    if action == "列表":
        items = repo.list_group_lore(group_id)
        if not items:
            await lore_cmd.finish("本群还没有群梗。\n" + LORE_HELP)
        lines = [f"#{item.id} [{'、'.join(item.keys)}] {item.content[:40]}" for item in items]
        await lore_cmd.finish("📒 本群群梗：\n" + "\n".join(lines))

    if action == "删除" and len(parts) >= 2 and parts[1].lstrip("#").isdigit():
        lore_id = int(parts[1].lstrip("#"))
        item = repo.get_group_lore(group_id, lore_id)
        if not item:
            await lore_cmd.finish(f"本群没有 #{lore_id} 这条群梗。")
        if item.created_by != event.get_user_id() and not await _is_admin(bot, event):
            await lore_cmd.finish("只有添加者本人或群管理员可以删除这条群梗。")
        repo.delete_group_lore(group_id, lore_id)
        await lore_cmd.finish(f"✅ 已删除群梗 #{lore_id}")

    await lore_cmd.finish(LORE_HELP)


@alias_cmd.handle()
async def handle_alias(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    scope = members.member_scope_for_group(str(event.group_id))
    target = _at_target(args) or event.get_user_id()
    words = args.extract_plain_text().split()
    action = words[0] if words else ""
    alias = " ".join(words[1:]).strip()

    if target != event.get_user_id() and action in ("添加", "删除") and not await _is_admin(bot, event):
        await alias_cmd.finish("给别人改昵称需要群管理员权限；给自己改不需要 @。")

    if action == "添加" and alias:
        if len(alias) < 2:
            await alias_cmd.finish("昵称至少 2 个字，太短容易误认。")
        repo.add_member_alias(scope, target, alias, "manual", time.time())
        others = [uid for uid in repo.find_members_by_alias(scope, alias) if uid != target]
        note = f"\n⚠️ 这个昵称也对应其他人（{', '.join(others)}），她会结合上下文判断。" if others else ""
        await alias_cmd.finish(f"✅ 已记住 {target} 也叫「{alias}」。{note}")

    if action == "删除" and alias:
        removed = repo.remove_member_alias(scope, target, alias)
        await alias_cmd.finish("✅ 已删除。" if removed else f"没有找到昵称「{alias}」。")

    profile = repo.get_member(scope, target)
    names = "、".join(profile.aliases) if profile and profile.aliases else "（还没有记录）"
    await alias_cmd.finish(
        f"{target} 的称呼：{names}\n"
        "用法：/dice 昵称 添加|删除 昵称 [@某人]"
    )


@profile_cmd.handle()
async def handle_profile(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    scope = members.member_scope_for_group(str(event.group_id))
    target = _at_target(args) or event.get_user_id()
    role_id = persona_manager.get_current_role_id(event)
    fav = get_user_favorability(members.affinity_key(target, str(event.group_id)), role_id=role_id)
    text = profiles.describe(repo.get_member(scope, target), fav)
    scope_note = "所有群共享" if scope == "global" else "仅本群"
    await profile_cmd.finish(f"🗂 {persona_manager.get_role_name(role_id)} 眼中的 {target}（{scope_note}）\n{text or '还不认识这个人。'}")


@forget_cmd.handle()
async def handle_forget(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    scope = members.member_scope_for_group(str(event.group_id))
    target = _at_target(args) or event.get_user_id()
    if target != event.get_user_id() and not await _is_admin(bot, event):
        await forget_cmd.finish("只能让她忘记你自己；忘记别人需要群管理员权限。")
    repo.forget_member(scope, target)
    await forget_cmd.finish(f"✅ 已清除对 {target} 的印象和记住的事（手动设置的昵称保留）。")


MEMORY_HELP = (
    "群记忆用法：\n"
    "/dice 群记忆            查看近期群况和最近的事件\n"
    "/dice 群记忆 搜索 关键词  看她会想起什么\n"
    "/dice 群记忆 删除 编号    让她忘掉某件事（群管理员）\n"
    "/dice 群记忆 整理        立刻整理一次（群管理员）\n"
    "/dice 群记忆 清空        清空本群的群记忆（群管理员）"
)


@memory_cmd.handle()
async def handle_memory(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = str(event.group_id)
    parts = args.extract_plain_text().strip().split(maxsplit=1)
    action = parts[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if action in ("删除", "整理", "清空") and not await _is_admin(bot, event):
        await memory_cmd.finish("需要群管理员权限。")

    if action == "删除" and rest.lstrip("#").isdigit():
        removed = repo.archive_group_event(group_id, int(rest.lstrip("#")))
        await memory_cmd.finish("✅ 已忘掉这件事。" if removed else "没有找到这条记忆。")

    if action == "整理":
        role_name = persona_manager.get_role_name(persona_manager.get_current_role_id(event))
        ok = await group_memory.digest_group(group_id, role_name)
        await memory_cmd.finish("✅ 已整理。" if ok else "没有需要整理的新消息，或者 AI 暂时不可用。")

    if action == "清空":
        repo.clear_group_memory(group_id)
        await memory_cmd.finish("✅ 已清空本群的群记忆（群梗和群员档案不受影响）。")

    if action == "搜索" and rest:
        found = await group_memory.recall(group_id, rest, [])
        if not found:
            await memory_cmd.finish("她什么也没想起来。")
        lines = [f"#{e.id} [重要度{e.importance}] {e.content}" for e in found]
        await memory_cmd.finish("🔍 她会想起：\n" + "\n".join(lines))

    if action:
        await memory_cmd.finish(MEMORY_HELP)

    digest = repo.get_group_digest(group_id)
    events = repo.list_group_events(group_id)[-8:]
    lines = [f"#{e.id} [重要度{e.importance}] {e.content}" for e in reversed(events)]
    vector_note = "（语义检索已开启）" if embedding_enabled() else "（仅关键词检索）"
    await memory_cmd.finish(
        f"🧠 本群记忆{vector_note}\n"
        f"近期群况：{digest.summary or '暂无'}\n\n"
        f"最近的事件：\n{chr(10).join(lines) or '暂无'}\n\n{MEMORY_HELP}"
    )


_SWITCHES = {"聊天": "enabled", "话题": "topic_reply"}


@group_switch_cmd.handle()
async def handle_group_switch(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = str(event.group_id)
    words = args.extract_plain_text().split()
    if len(words) == 2 and words[0] in _SWITCHES and words[1] in ("开", "关"):
        if not await _is_admin(bot, event):
            await group_switch_cmd.finish("需要群管理员权限。")
        repo.set_group_chat_setting(group_id, _SWITCHES[words[0]], words[1] == "开")
        await group_switch_cmd.finish(f"✅ 本群「{words[0]}」已{words[1]}启。")

    settings = repo.get_group_chat_settings(group_id)

    def flag(value: bool) -> str:
        return "开" if value else "关"

    await group_switch_cmd.finish(
        "本群设置：\n"
        f"聊天：{flag(settings.enabled)}（关掉后她在本群只做投骰）\n"
        f"话题：{flag(settings.topic_reply)}（聊到群梗 / 她感兴趣的话题时可能接话）\n"
        "用法：/dice 群设置 聊天|话题 开|关"
    )


@reload_cmd.handle()
async def handle_reload():
    roles, lore = persona_manager.reload()
    await reload_cmd.finish(f"✅ 已重新读取角色卡和世界书：{roles} 个角色，{lore} 条世界书。")
