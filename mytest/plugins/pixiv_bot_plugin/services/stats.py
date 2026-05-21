def build_stats_message(spider) -> str:
    authors_info = spider.get_favorite_authors_info()
    tags_info = spider.get_tag_pools_info()

    message = "📊 使用统计\n\n"
    message += f"👥 关注作者：{authors_info.get('total_count', 0)}个\n"
    message += f"🆕 最近添加：{authors_info.get('recent_added', 0)}个\n"
    message += f"👀 未检查：{authors_info.get('never_checked', 0)}个\n"
    message += f"🏷️ 总圈名：{authors_info.get('total_aliases', 0)}个\n\n"
    message += f"❤️ 喜好标签：{tags_info.get('preferred_count', 0)}个\n"
    message += f"🚫 屏蔽标签：{tags_info.get('blocked_count', 0)}个"
    return message


def build_popular_tags_message(spider) -> str:
    tags_info = spider.get_tag_pools_info()
    preferred = tags_info.get("preferred_tags", [])

    if not preferred:
        return "还没有设置喜好标签"

    message = "🔥 热门标签（你的喜好）\n\n"
    for i, tag in enumerate(preferred[:10], 1):
        message += f"{i}. {tag}\n"

    if len(preferred) > 10:
        message += f"\n... 还有{len(preferred) - 10}个标签"
    return message
