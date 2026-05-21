def handle_preference(spider, action: str, tag: str | None) -> str:
    if action == "查看":
        info = spider.get_tag_pools_info()
        preferred = info["preferred_tags"]
        blocked = info["blocked_tags"]

        message = "🎨 内容偏好设置\n\n"
        message += f"❤️ 喜好标签（{info['preferred_count']}个）：\n"
        message += "、".join(preferred) if preferred else "无"
        message += f"\n\n🚫 屏蔽标签（{info['blocked_count']}个）：\n"
        message += "、".join(blocked) if blocked else "无"
        return message

    if action in ["喜欢", "加喜好", "添加喜好"]:
        if not tag:
            return "请指定标签，例如：p站 偏好 喜欢 风景"
        return f"✅ 已添加喜好标签：{tag}" if spider.add_preferred_tag(tag) else "❌ 添加喜好标签失败"

    if action in ["不喜欢", "删喜好", "移除喜好"]:
        if not tag:
            return "请指定标签，例如：p站 偏好 不喜欢 风景"
        return f"✅ 已移除喜好标签：{tag}" if spider.remove_preferred_tag(tag) else "❌ 移除喜好标签失败"

    if action in ["屏蔽", "加厌恶", "添加厌恶"]:
        if not tag:
            return "请指定标签，例如：p站 偏好 屏蔽 R-18"
        return f"✅ 已添加屏蔽标签：{tag}" if spider.add_blocked_tag(tag) else "❌ 添加屏蔽标签失败"

    if action in ["不屏蔽", "删厌恶", "移除厌恶"]:
        if not tag:
            return "请指定标签，例如：p站 偏好 不屏蔽 R-18"
        return f"✅ 已移除屏蔽标签：{tag}" if spider.remove_blocked_tag(tag) else "❌ 移除屏蔽标签失败"

    return """
🎨 内容偏好管理帮助：

📋 p站 偏好 查看 - 查看当前偏好设置
❤️ p站 偏好 喜欢 [标签] - 添加喜好标签
💔 p站 偏好 不喜欢 [标签] - 移除喜好标签
🚫 p站 偏好 屏蔽 [标签] - 添加屏蔽标签
✅ p站 偏好 不屏蔽 [标签] - 移除屏蔽标签

示例：
• p站 偏好 喜欢 风景
• p站 偏好 屏蔽 R-18
• p站 偏好 查看
    """.strip()


def handle_alias(spider, action: str, user_id: str, alias: str | None) -> str:
    if action == "列表":
        aliases = spider.get_all_aliases()
        if not aliases:
            return "还没有设置任何圈名"

        message = "📋 圈名列表\n\n"
        for alias_name, author_id in aliases.items():
            author_info = spider.get_favorite_author_info(author_id)
            author_name = author_info.get("name", "未知") if author_info else "未知"
            message += f"• {alias_name} → {author_name}（{author_id}）\n"
        return message

    if action == "设置":
        if not user_id or not alias:
            return "请指定作者ID和圈名，例如：p站 圈名 设置 123456 老王"
        if spider.add_author_alias(user_id, alias):
            return f"✅ 已为作者（{user_id}）设置圈名：{alias}"
        return "❌ 设置圈名失败，可能圈名已存在或作者不在关注列表中"

    if action == "删除":
        if not alias:
            return "请指定要删除的圈名，例如：p站 圈名 删除 老王"

        found_user_id = spider.search_author_by_alias(alias)
        if not found_user_id:
            return f"❌ 未找到圈名「{alias}」"

        return f"✅ 已删除圈名：{alias}" if spider.remove_author_alias(found_user_id, alias) else "❌ 删除圈名失败"

    if action == "搜索":
        if not alias:
            return "请指定要搜索的圈名，例如：p站 圈名 搜索 老王"

        found_user_id = spider.search_author_by_alias(alias)
        if found_user_id:
            author_info = spider.get_favorite_author_info(found_user_id)
            author_name = author_info.get("name", "未知") if author_info else "未知"
            return f"找到圈名「{alias}」对应的作者：{author_name}（{found_user_id}）"
        return f"❌ 未找到圈名「{alias}」"

    return """
🏷️ 圈名管理帮助：

📋 p站 圈名 列表 - 查看所有圈名
⚙️ p站 圈名 设置 [作者ID] [圈名] - 为作者设置圈名
🗑️ p站 圈名 删除 [圈名] - 删除圈名
🔍 p站 圈名 搜索 [圈名] - 通过圈名查找作者

示例：
• p站 圈名 设置 123456 老王
• p站 圈名 删除 老王
• p站 圈名 搜索 老王
• p站 圈名 列表
    """.strip()
