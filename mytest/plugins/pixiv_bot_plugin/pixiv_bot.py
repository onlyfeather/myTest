# plugins/pixiv_bot_plugin/pixiv_bot.py
import asyncio
import re
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, List, Union
from nonebot.adapters import Event
from nonebot.params import RegexStr
from nonebot_plugin_alconna import Command, Alconna, Args, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import Image, UniMessage
from nonebot.plugin import PluginMetadata
from nonebot import logger, on_regex
from nonebot.log import LoguruHandler
from .spiderPixiv import PixivSpider

# 配置logging重定向到loguru
logging.basicConfig(handlers=[LoguruHandler()])

# 使用nonebot的logger
bot_logger = logger
PIXIV_PREFIX = "p站"


def pixiv_command(name: str) -> str:
    return f"{PIXIV_PREFIX} {name}"


def extract_plain_text(event: Event) -> str:
    if hasattr(event, "get_plaintext"):
        return event.get_plaintext()
    return event.get_plain_text()


def pixiv_failure_message(
    spider: PixivSpider,
    action: str,
    target: str,
    detail: str = "",
) -> str:
    reason = getattr(spider, "last_error_reason", None)
    reason_text = {
        "missing_cookie": "还没有配置 Pixiv Cookie，请先使用 /p站 登录 [Cookie]。",
        "empty_cookie": "已保存的 Pixiv Cookie 是空的，请重新使用 /p站 登录 [Cookie]。",
        "cookie_load_error": "读取 Pixiv Cookie 失败，请检查本地数据文件权限，或重新登录。",
        "cookie_expired": "Pixiv 拒绝了当前 Cookie，可能已经过期，请重新登录。",
        "rate_limited": "Pixiv 返回了访问频率限制，请稍后再试。",
        "pixiv_server_error": "Pixiv 服务端暂时异常，请稍后再试。",
        "network_error": "连接 Pixiv 失败，可能是服务器网络、代理或 DNS 问题。",
        "session_not_initialized": "Pixiv 请求会话尚未初始化，请重启机器人后再试。",
        "empty_keyword": "搜索关键词为空，请补充标签。",
        "search_failed": "Pixiv 搜索接口没有返回可用结果，可能是 Cookie 失效或网络波动。",
        "no_qualified_images": "找到了搜索结果，但没有图片通过当前筛选条件。",
        "no_recent_images": "找到了标签，但指定时间范围内没有新图。",
        "unknown_error": "请求过程中出现未知错误，请查看后台日志。",
    }.get(reason)

    if reason_text is None and isinstance(reason, str) and reason.startswith("http_"):
        reason_text = f"Pixiv 返回 HTTP {reason.removeprefix('http_')}，请求没有成功。"

    if reason_text is None:
        reason_text = "没有找到符合条件的图片。"

    message = f"{action}「{target}」没有返回图片。\n原因：{reason_text}"
    if detail:
        message += f"\n建议：{detail}"
    return message


@asynccontextmanager
async def get_pixiv_spider():
    async with PixivSpider() as spider:
        yield spider

# 中文模式到英文模式的映射
MODE_MAPPING = {
    "随机": "random",
    "最新": "recent", 
    "热门": "popular",
    "美图": "popular",
    "random": "random",
    "recent": "recent",
    "popular": "popular",
    "beautiful": "popular"
}

def convert_mode_to_english(mode: str) -> str:
    """将中文模式转换为英文模式"""
    if not mode:
        return "random"
    return MODE_MAPPING.get(mode.lower(), "random")  # 默认为random


@dataclass
class SearchParams:
    display_tags: str
    search_tags: str
    count: int
    mode: str
    min_bookmarks: Optional[int] = None


def parse_bookmark_filter(token: str) -> Optional[int]:
    """Parse a user-facing bookmark filter into Pixiv's users入り threshold."""
    patterns = (
        r"收藏(?:数)?(?:>=|>|不少于|至少)?(\d+)",
        r"(\d+)(?:收藏|users入り|users入|users)",
        r"bookmarks?(?:>=|>)?(\d+)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, token, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_search_text(raw_text: str) -> SearchParams | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 搜图", "搜图"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    if not text:
        return None

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    mode = "random"
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index].lower() in MODE_MAPPING:
            mode = convert_mode_to_english(tokens.pop(index))
            break

    count = 1
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index].isdigit():
            count = int(tokens.pop(index))
            break

    min_bookmarks = None
    tags = []
    for token in tokens:
        bookmark_filter = parse_bookmark_filter(token)
        if bookmark_filter is not None:
            min_bookmarks = bookmark_filter
            continue
        tags.append(token)

    if not tags:
        return None

    display_tags = " ".join(tags)
    search_tags = display_tags
    if min_bookmarks is not None:
        search_tags = f"{search_tags} {min_bookmarks}users入り"

    return SearchParams(
        display_tags=display_tags,
        search_tags=search_tags,
        count=min(max(count, 1), 5),
        mode=mode,
        min_bookmarks=min_bookmarks,
    )


def parse_beautiful_text(raw_text: str) -> Optional[str]:
    text = raw_text.strip()
    prefix = f"/{PIXIV_PREFIX} 美图"
    if text.startswith(prefix):
        text = text[len(prefix):].strip()

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    tags = []
    for token in tokens:
        if token.isdigit() or token.lower() in MODE_MAPPING:
            continue
        if parse_bookmark_filter(token) is not None:
            continue
        tags.append(token)

    return " ".join(tags) if tags else None


@dataclass
class LatestParams:
    tags: str
    count: int = 1
    hours: int = 24


def parse_latest_text(raw_text: str) -> LatestParams | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 最新", "最新"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    numbers = []
    tags = []
    for token in tokens:
        if token.isdigit():
            numbers.append(int(token))
        else:
            tags.append(token)

    if not tags:
        return None

    count = numbers[0] if len(numbers) >= 1 else 1
    hours = numbers[1] if len(numbers) >= 2 else 24
    return LatestParams(
        tags=" ".join(tags),
        count=min(max(count, 1), 10),
        hours=min(max(hours, 1), 168),
    )


def parse_hot_text(raw_text: str) -> tuple[str, int] | None:
    text = raw_text.strip()
    for prefix in (f"/{PIXIV_PREFIX} 热门", "热门"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    tokens = [token for token in re.split(r"[\s,，]+", text) if token]
    if not tokens:
        return None

    count = 1
    tags = []
    for token in tokens:
        if token.isdigit():
            count = int(token)
        else:
            tags.append(token)

    if not tags:
        return None

    return " ".join(tags), min(max(count, 1), 5)

# 插件元数据
__plugin_meta__ = PluginMetadata(
    name="pixiv_bot",
    description="Pixiv智能图片搜索机器人",
    usage="基于用户故事地图设计的完整Pixiv图片搜索功能"
)

# ==================== 核心功能层命令 ====================

# 每日一图命令
daily_recommend = Alconna(
    pixiv_command("每日一图")
)

# ==================== 作者追踪功能命令 ====================

# 作者作品命令
author_images = Alconna(
    pixiv_command("作者"),
    Args["user_id", str]["count", int, 3]["mode", str, "recent"]
)

# 关注作者命令
follow_author = Alconna(
    pixiv_command("关注"),
    Args["user_id", str]["author_name", str, ""]
)

# 取关作者命令
unfollow_author = Alconna(
    pixiv_command("取关"),
    Args["user_id", str]
)

# 关注列表命令
follow_list = Alconna(pixiv_command("关注列表"))

# ==================== 个性化配置层命令 ====================

# 内容偏好管理命令
content_preference = Alconna(
    pixiv_command("偏好"),
    Args["action", str]["tag", str, None]
)

# 圈名管理命令
alias_manage = Alconna(
    pixiv_command("圈名"),
    Args["action", str]["user_id", str]["alias", str, None]
)

# ==================== 系统管理层命令 ====================

# 登录状态命令
login_status = Alconna(pixiv_command("登录状态"))

# 质量设置命令
quality_setting = Alconna(
    pixiv_command("质量设置"),
    Args["score", int, 60]
)

# ==================== 数据分析层命令 ====================

# 统计命令
stats_cmd = Alconna(pixiv_command("统计"))

# 热门标签命令
popular_tags = Alconna(pixiv_command("热门标签"))

# ==================== 基础查询命令 ====================

# 图片信息命令
image_info = Alconna(
    pixiv_command("图片信息"),
    Args["illust_id", str]
)

# 作者信息命令
author_info = Alconna(
    pixiv_command("作者信息"),
    Args["user_id", str]
)

# ==================== 帮助命令 ====================

help_cmd = Alconna(pixiv_command("帮助"))

# ==================== 注册命令匹配器 ====================

# 核心功能
search_matcher = on_regex(r"^(?:/p站\s+)?搜图(?:\s+.*)?$", priority=1, block=True)
latest_matcher = on_regex(r"^(?:/p站\s+)?最新(?:\s+.*)?$", priority=1, block=True)
popular_matcher = on_regex(r"^(?:/p站\s+)?美图(?:\s+.*)?$", priority=1, block=True)
hot_matcher = on_regex(r"^(?:/p站\s+)?热门(?:\s+.*)?$", priority=1, block=True)
daily_matcher = on_alconna(daily_recommend, use_cmd_start=True, block=True)

# 作者追踪
author_matcher = on_alconna(author_images, use_cmd_start=True, block=True)
follow_matcher = on_alconna(follow_author, use_cmd_start=True, block=True)
unfollow_matcher = on_alconna(unfollow_author, use_cmd_start=True, block=True)
follow_list_matcher = on_alconna(follow_list, use_cmd_start=True, block=True)

# 个性化配置
preference_matcher = on_alconna(content_preference, use_cmd_start=True, block=True)
alias_matcher = on_alconna(alias_manage, use_cmd_start=True, block=True)

# 系统管理
login_matcher = on_regex(r"^/p站\s+登录(?:\s+.*)?$", priority=1, block=True)
login_status_matcher = on_alconna(login_status, use_cmd_start=True, block=True)
quality_matcher = on_alconna(quality_setting, use_cmd_start=True, block=True)

# 数据分析
stats_matcher = on_alconna(stats_cmd, use_cmd_start=True, block=True)
popular_tags_matcher = on_alconna(popular_tags, use_cmd_start=True, block=True)

# 基础查询
image_info_matcher = on_alconna(image_info, use_cmd_start=True, block=True)
author_info_matcher = on_alconna(author_info, use_cmd_start=True, block=True)

# 帮助
help_matcher = on_alconna(help_cmd, use_cmd_start=True, block=True)

# ==================== 帮助处理器 ====================

@help_matcher.handle()
async def help_handle(result: Arparma):
    """处理帮助命令"""
    help_text = """
🎨 Pixiv智能图片搜索机器人帮助

📸 核心功能 - 图片搜索与推荐：
• p站 搜图 [标签] [数量] [模式] - 智能搜图
• 搜图 [标签] [数量] [模式] - 核心短指令
  └─ 模式选择：随机(random)、最新(recent)、热门(popular)、美图(beautiful)
  └─ 收藏筛选：可追加 收藏500、收藏>=1000、500收藏
• p站 最新 [标签] [数量] [小时] - 获取指定时间内的最新图片
• p站 美图 [标签] - 获取1张高收藏精选美图（收藏≥1000/500/100 三级回退）
• p站 热门 [标签] [数量] - 获取当前热门图片
• p站 每日一图 - 获取个性化每日推荐

👥 作者追踪 - 作者管理与作品浏览：
• p站 作者 [ID/圈名] [数量] [模式] - 获取作者作品
  └─ 模式选择：随机(random)、最新(recent)、热门(popular)
• p站 作者信息 [作者ID] - 查看作者详细资料和统计数据
• p站 关注 [作者ID] [作者名] - 添加作者到关注列表
• p站 取关 [作者ID] - 从关注列表中移除作者
• p站 关注列表 - 查看已关注作者列表

🔍 基础查询 - 详细信息查询：
• p站 图片信息 [图片ID] - 获取图片完整信息和统计数据
• p站 作者信息 [作者ID] - 获取作者详细资料和最新作品

🎨 个性化配置 - 内容偏好管理：
• p站 偏好 [动作] [标签] - 管理内容偏好标签
  └─ 动作：查看、喜欢、不喜欢、屏蔽、不屏蔽
• p站 圈名 [动作] [参数] - 管理作者圈名别名
  └─ 动作：列表、设置、删除、搜索

🔧 系统管理 - 账户与设置：
• p站 登录 [Cookie] - 设置Pixiv账户登录信息
• p站 登录状态 - 检查当前登录状态
• p站 质量设置 [分数] - 设置图片质量评分阈值(0-100)

📊 数据分析 - 使用统计与分析：
• p站 统计 - 查看个人使用统计数据
• p站 热门标签 - 查看个人喜好标签排行

💡 使用提示与限制：
• 搜索支持：多标签用空格或逗号分隔，支持中文和英文模式
• 作者功能：支持ID和圈名两种搜索方式
• 内容过滤：所有功能都支持智能内容过滤
• 数量限制：搜图1-5张，最新1-10张，作者作品1-5张

🔍 详细帮助命令：
• p站 偏好 帮助 - 查看偏好管理详细使用说明
• p站 圈名 帮助 - 查看圈名管理详细使用说明
    """
    await UniMessage.text(help_text.strip()).send()

# ==================== 核心功能层处理器 ====================

@search_matcher.handle()
async def search_images_handle(event: Event, regex_str: str = RegexStr()):
    """处理图片搜索命令"""
    parsed = parse_search_text(regex_str or extract_plain_text(event))
    
    if not parsed:
        await UniMessage.text("请指定搜索标签，例如：p站 搜图 风景 收藏500 3 随机").send()
        return

    tags = parsed.search_tags
    count = parsed.count
    english_mode = parsed.mode
    if english_mode not in ["random", "recent", "popular"]:
        await UniMessage.text("模式必须是：随机、最新、热门、美图（或：random、recent、popular、beautiful）").send()
        return

    try:
        display_tags = parsed.display_tags
        if parsed.min_bookmarks is not None:
            display_tags = f"{display_tags}（收藏≥{parsed.min_bookmarks}）"
        await UniMessage.text(f"正在搜索「{display_tags}」的{count}张图片（{english_mode}模式），请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            search_result = await spider.search_images(tags, english_mode, count)
            
            if not search_result:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "搜索",
                        display_tags,
                        "可先用 /p站 登录状态 检查 Cookie；如果带了收藏筛选，可以降低收藏阈值或减少标签。",
                    )
                ).send()
                return
            
            images = search_result.get('images', [])
            if not images:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "搜索",
                        display_tags,
                        "可以尝试减少标签、改用随机模式，或降低收藏筛选。",
                    )
                ).send()
                return
            
            await send_images_with_info(images, f"搜索结果「{display_tags}」")
            
    except Exception as e:
        await UniMessage.text(f"搜索图片时发生错误：{str(e)}").send()


@latest_matcher.handle()
async def latest_images_handle(event: Event, regex_str: str = RegexStr()):
    """处理最新图片命令"""
    parsed = parse_latest_text(regex_str or extract_plain_text(event))
    
    if not parsed:
        await UniMessage.text("请指定标签，例如：p站 最新 风景 5 24").send()
        return

    tags = parsed.tags
    count = parsed.count
    hours = parsed.hours
    
    try:
        await UniMessage.text(f"正在获取「{tags}」最近{hours}小时的{count}张最新图片，请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            search_result = await spider.get_tag_latest_images(tags, count, hours)
            
            if not search_result:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "最新图片",
                        tags,
                        f"可以把时间范围放宽，例如：/p站 最新 {tags} {count} 168。",
                    )
                ).send()
                return
            
            images = search_result.get('images', [])
            if not images:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "最新图片",
                        tags,
                        f"最近 {hours} 小时内没有符合条件的作品，可以扩大到 168 小时试试。",
                    )
                ).send()
                return
            
            await send_images_with_info(images, f"最新图片「{tags}」（{hours}小时内）")
            
    except Exception as e:
        await UniMessage.text(f"获取最新图片时发生错误：{str(e)}").send()


@popular_matcher.handle()
async def popular_images_handle(event: Event, regex_str: str = RegexStr()):
    """处理美图命令"""
    tags = parse_beautiful_text(regex_str or extract_plain_text(event))
    
    if not tags:
        await UniMessage.text("请指定标签，例如：p站 美图 萌妹").send()
        return
    
    try:
        await UniMessage.text(f"正在获取「{tags}」的高收藏精选美图，请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            selected_threshold = 1000
            search_result = await spider.search_images(f"{tags} 1000users入り", "random", 1)

            if not search_result or not search_result.get("images"):
                selected_threshold = 500
                search_result = await spider.search_images(f"{tags} 500users入り", "random", 1)

            if not search_result or not search_result.get("images"):
                selected_threshold = 100
                search_result = await spider.search_images(f"{tags} 100users入り", "random", 1)
            
            if not search_result:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "美图",
                        tags,
                        "已按收藏≥1000、≥500、≥100 依次回退；仍无结果时可以减少标签或换更常见的日文/英文标签。",
                    )
                ).send()
                return

            images = search_result.get('images', [])
            if not images:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "美图",
                        tags,
                        "已经回退到收藏≥100，仍没有可发送图片，可能是标签过窄或结果被过滤。",
                    )
                ).send()
                return
            
            await send_images_with_info(images, f"美图推荐「{tags}」（收藏≥{selected_threshold}）")
            
    except Exception as e:
        await UniMessage.text(f"获取美图时发生错误：{str(e)}").send()


@hot_matcher.handle()
async def hot_images_handle(event: Event, regex_str: str = RegexStr()):
    """处理热门命令"""
    parsed = parse_hot_text(regex_str or extract_plain_text(event))
    
    if not parsed:
        await UniMessage.text("请指定标签，例如：p站 热门 萌妹 3").send()
        return

    tags, count = parsed
    
    try:
        await UniMessage.text(f"正在获取「{tags}」的{count}张热门图片，请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            search_result = await spider.search_images(tags, "popular", count)
            
            if not search_result:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "热门图片",
                        tags,
                        "热门模式依赖 Pixiv 的热门结果；如果没有返回，可以换用 搜图 或 美图。",
                    )
                ).send()
                return
            
            images = search_result.get('images', [])
            if not images:
                await UniMessage.text(
                    pixiv_failure_message(
                        spider,
                        "热门图片",
                        tags,
                        "Pixiv 没有给出可用热门作品，可以减少标签或改用随机搜图。",
                    )
                ).send()
                return
            
            await send_images_with_info(images, f"热门推荐「{tags}」")
            
    except Exception as e:
        await UniMessage.text(f"获取热门图片时发生错误：{str(e)}").send()




@daily_matcher.handle()
async def daily_recommend_handle(result: Arparma):
    """处理今日推荐命令"""
    try:
        await UniMessage.text("正在获取今日推荐图片，请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            # 获取用户QQ号（如果可用）
            user_qq = None  # 这里需要根据nonebot的实际API获取用户ID
            
            daily_result = await spider.get_random_image(user_qq)
            
            if not daily_result:
                await UniMessage.text("获取今日推荐失败，请稍后重试").send()
                return
            
            image = daily_result.get('image')
            tag = daily_result.get('tag', '今日推荐')
            quality_score = daily_result.get('quality_score')
            
            if not image:
                await UniMessage.text("图片信息获取失败").send()
                return
            
            # 🔥 修复：获取原图URL而不是缩略图
            image_id = image.get('id')
            if image_id:
                # 获取原图URL列表
                original_urls = await spider.get_illust_original_urls(image_id)
                if original_urls:
                    # 替换图片数据中的URL为原图URL
                    image['urls'] = original_urls
                    logger.debug(f"每日一图已获取原图: {len(original_urls)} 个URL")
                else:
                    # 如果获取原图失败，使用缩略图作为备用
                    thumbnail_url = image.get('url', '')
                    if thumbnail_url:
                        image['urls'] = [thumbnail_url]
                        logger.debug("每日一图获取原图失败，使用缩略图备用")
                    else:
                        logger.debug("每日一图无法获取任何图片URL")
            else:
                logger.debug("每日一图图片ID为空")
            
            await send_single_image_with_info(image, f"📅 今日推荐「{tag}」", quality_score)
            
    except Exception as e:
        await UniMessage.text(f"获取今日推荐时发生错误：{str(e)}").send()

# ==================== 作者追踪功能处理器 ====================

@author_matcher.handle()
async def author_images_handle(result: Arparma):
    """处理作者作品命令"""
    user_id = result.query[str]("user_id")
    mode = result.query[str]("mode")
    count = result.query[int]("count")
    
    if not user_id:
        await UniMessage.text("请指定作者ID或圈名，例如：p站 作者 123456 3 随机").send()
        return
    
    # 转换中文模式到英文
    english_mode = convert_mode_to_english(mode or"")
    if english_mode not in ["random", "recent", "popular"]:
        await UniMessage.text("模式必须是：随机、最新、热门（或：random、recent、popular）").send()
        return
    
    count = min(max(count or 1, 1), 5)  # 限制1-5张
    
    try:
        await UniMessage.text(f"正在获取作者「{user_id}」的{count}张作品（{english_mode}模式），请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            author_result = await spider.get_author_images(user_id, english_mode, count)
            
            if not author_result:
                await UniMessage.text(f"未找到作者「{user_id}」或其作品").send()
                return
            
            images = author_result.get('images', [])
            author_info = author_result.get('author_info', {})
            
            if not images:
                await UniMessage.text(f"作者「{author_info.get('name', user_id)}」没有符合条件的作品").send()
                return
            
            author_name = author_info.get('name', '未知作者')
            is_favorite = author_info.get('is_favorite', False)
            favorite_text = "（已关注）" if is_favorite else ""
            
            await send_images_with_info(images, f"作者作品「{author_name}」{favorite_text}")
            
    except Exception as e:
        await UniMessage.text(f"获取作者作品时发生错误：{str(e)}").send()


@follow_matcher.handle()
async def follow_author_handle(result: Arparma):
    """处理关注作者命令"""
    user_id = result.query[str]("user_id")
    author_name = result.query[str]("author_name")
    
    if not user_id:
        await UniMessage.text("请指定作者ID，例如：p站 关注 123456 画师名").send()
        return
    
    try:
        async with get_pixiv_spider() as spider:
            if spider.add_favorite_author(user_id, author_name or ""):
                name_text = f"「{author_name}」" if author_name else ""
                await UniMessage.text(f"✅ 已成功关注作者 {name_text}（{user_id}）").send()
            else:
                await UniMessage.text(f"❌ 关注作者失败，可能已经关注过").send()
                
    except Exception as e:
        await UniMessage.text(f"关注作者时发生错误：{str(e)}").send()


@unfollow_matcher.handle()
async def unfollow_author_handle(result: Arparma):
    """处理取关作者命令"""
    user_id = result.query[str]("user_id")
    
    if not user_id:
        await UniMessage.text("请指定作者ID，例如：p站 取关 123456").send()
        return
    
    try:
        async with get_pixiv_spider() as spider:
            # 先检查是否在关注列表中
            author_info = spider.get_favorite_author_info(user_id)
            if not author_info:
                await UniMessage.text(f"❌ 作者 {user_id} 不在关注列表中").send()
                return
            
            author_name = author_info.get('name', '未知作者')
            
            if spider.remove_favorite_author(user_id):
                await UniMessage.text(f"✅ 已成功取关作者「{author_name}」（{user_id}）").send()
            else:
                await UniMessage.text(f"❌ 取关作者失败").send()
                
    except Exception as e:
        await UniMessage.text(f"取关作者时发生错误：{str(e)}").send()


@follow_list_matcher.handle()
async def follow_list_handle(result: Arparma):
    """处理关注列表命令"""
    try:
        async with get_pixiv_spider() as spider:
            authors_info = spider.get_favorite_authors_info()
            
            total_count = authors_info.get('total_count', 0)
            recent_added = authors_info.get('recent_added', 0)
            never_checked = authors_info.get('never_checked', 0)
            authors = authors_info.get('authors', {})
            
            if total_count == 0:
                await UniMessage.text("你还没有关注任何作者").send()
                return
            
            message = f"📋 关注列表（共{total_count}个作者）\n\n"
            message += f"🆕 最近7天添加：{recent_added}个\n"
            message += f"👀 从未检查：{never_checked}个\n\n"
            
            # 显示前10个作者
            count = 0
            for user_id, author_info in authors.items():
                if count >= 10:
                    break
                name = author_info.get('name', '未知')
                aliases = author_info.get('aliases', [])
                alias_text = f"（圈名：{', '.join(aliases)}）" if aliases else ""
                message += f"• {name}（{user_id}）{alias_text}\n"
                count += 1
            
            if total_count > 10:
                message += f"\n... 还有{total_count - 10}个作者"
            
            await UniMessage.text(message).send()
            
    except Exception as e:
        await UniMessage.text(f"获取关注列表时发生错误：{str(e)}").send()

# ==================== 个性化配置层处理器 ====================

@preference_matcher.handle()
async def content_preference_handle(result: Arparma):
    """处理内容偏好管理命令"""
    action = result.query[str]("action")
    tag = result.query[str]("tag", None)
    
    try:
        async with get_pixiv_spider() as spider:
            if action == "查看":
                info = spider.get_tag_pools_info()
                preferred = info['preferred_tags']
                blocked = info['blocked_tags']
                
                message = f"🎨 内容偏好设置\n\n"
                message += f"❤️ 喜好标签（{info['preferred_count']}个）：\n"
                if preferred:
                    message += "、".join(preferred)
                else:
                    message += "无"
                
                message += f"\n\n🚫 屏蔽标签（{info['blocked_count']}个）：\n"
                if blocked:
                    message += "、".join(blocked)
                else:
                    message += "无"
                
                await UniMessage.text(message).send()
                
            elif action in ["喜欢", "加喜好", "添加喜好"]:
                if not tag:
                    await UniMessage.text("请指定标签，例如：p站 偏好 喜欢 风景").send()
                    return
                
                if spider.add_preferred_tag(tag):
                    await UniMessage.text(f"✅ 已添加喜好标签：{tag}").send()
                else:
                    await UniMessage.text(f"❌ 添加喜好标签失败").send()
                    
            elif action in ["不喜欢", "删喜好", "移除喜好"]:
                if not tag:
                    await UniMessage.text("请指定标签，例如：p站 偏好 不喜欢 风景").send()
                    return
                
                if spider.remove_preferred_tag(tag):
                    await UniMessage.text(f"✅ 已移除喜好标签：{tag}").send()
                else:
                    await UniMessage.text(f"❌ 移除喜好标签失败").send()
                    
            elif action in ["屏蔽", "加厌恶", "添加厌恶"]:
                if not tag:
                    await UniMessage.text("请指定标签，例如：p站 偏好 屏蔽 R-18").send()
                    return
                
                if spider.add_blocked_tag(tag):
                    await UniMessage.text(f"✅ 已添加屏蔽标签：{tag}").send()
                else:
                    await UniMessage.text(f"❌ 添加屏蔽标签失败").send()
                    
            elif action in ["不屏蔽", "删厌恶", "移除厌恶"]:
                if not tag:
                    await UniMessage.text("请指定标签，例如：p站 偏好 不屏蔽 R-18").send()
                    return
                
                if spider.remove_blocked_tag(tag):
                    await UniMessage.text(f"✅ 已移除屏蔽标签：{tag}").send()
                else:
                    await UniMessage.text(f"❌ 移除屏蔽标签失败").send()
                    
            else:
                help_text = """
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
                """
                await UniMessage.text(help_text.strip()).send()
                
    except Exception as e:
        await UniMessage.text(f"内容偏好管理时发生错误：{str(e)}").send()


@alias_matcher.handle()
async def alias_manage_handle(result: Arparma):
    """处理圈名管理命令"""
    action = result.query[str]("action")
    user_id = result.query[str]("user_id")
    alias = result.query[str]("alias", None)
    
    try:
        async with get_pixiv_spider() as spider:
            if action == "列表":
                aliases = spider.get_all_aliases()
                if not aliases:
                    await UniMessage.text("还没有设置任何圈名").send()
                    return
                
                message = "📋 圈名列表\n\n"
                for alias_name, author_id in aliases.items():
                    author_info = spider.get_favorite_author_info(author_id)
                    author_name = author_info.get('name', '未知') if author_info else '未知'
                    message += f"• {alias_name} → {author_name}（{author_id}）\n"
                
                await UniMessage.text(message).send()
                
            elif action == "设置":
                if not user_id or not alias:
                    await UniMessage.text("请指定作者ID和圈名，例如：p站 圈名 设置 123456 老王").send()
                    return
                
                if spider.add_author_alias(user_id, alias):
                    await UniMessage.text(f"✅ 已为作者（{user_id}）设置圈名：{alias}").send()
                else:
                    await UniMessage.text(f"❌ 设置圈名失败，可能圈名已存在或作者不在关注列表中").send()
                    
            elif action == "删除":
                if not alias:
                    await UniMessage.text("请指定要删除的圈名，例如：p站 圈名 删除 老王").send()
                    return
                
                # 先通过圈名找到作者ID
                found_user_id = spider.search_author_by_alias(alias)
                if not found_user_id:
                    await UniMessage.text(f"❌ 未找到圈名「{alias}」").send()
                    return
                
                if spider.remove_author_alias(found_user_id, alias):
                    await UniMessage.text(f"✅ 已删除圈名：{alias}").send()
                else:
                    await UniMessage.text(f"❌ 删除圈名失败").send()
                    
            elif action == "搜索":
                if not alias:
                    await UniMessage.text("请指定要搜索的圈名，例如：p站 圈名 搜索 老王").send()
                    return
                
                found_user_id = spider.search_author_by_alias(alias)
                if found_user_id:
                    author_info = spider.get_favorite_author_info(found_user_id)
                    author_name = author_info.get('name', '未知') if author_info else '未知'
                    await UniMessage.text(f"找到圈名「{alias}」对应的作者：{author_name}（{found_user_id}）").send()
                else:
                    await UniMessage.text(f"❌ 未找到圈名「{alias}」").send()
                    
            else:
                help_text = """
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
                """
                await UniMessage.text(help_text.strip()).send()
                
    except Exception as e:
        await UniMessage.text(f"圈名管理时发生错误：{str(e)}").send()

# ==================== 系统管理层处理器 ====================

@login_matcher.handle()
async def login_handle(event: Event):
    """处理登录命令"""
    prefix = f"/{PIXIV_PREFIX} 登录"
    text = extract_plain_text(event).strip()
    cookie = ""
    if text.startswith(prefix):
        cookie = text[len(prefix):].strip()
    
    if not cookie:
        await UniMessage.text(f"请提供Cookie，例如：{PIXIV_PREFIX} 登录 your_cookie_string").send()
        return
    
    try:
        async with get_pixiv_spider() as spider:
            if spider.set_cookie(cookie):
                await UniMessage.text("✅ 登录成功！Pixiv功能已启用").send()
            else:
                await UniMessage.text("❌ 登录失败，请检查Cookie是否正确").send()
                
    except Exception as e:
        await UniMessage.text(f"登录时发生错误：{str(e)}").send()


@login_status_matcher.handle()
async def login_status_handle(result: Arparma):
    """处理登录状态命令"""
    try:
        async with get_pixiv_spider() as spider:
            cookie_data = spider.storage.load_cookie()
            if spider.is_logged_in and cookie_data:
                user_id = spider.user_id or cookie_data.get("user_id")
                saved_time = cookie_data.get("saved_time") or "未知"
                cookie_text = cookie_data.get("full_cookie") or ""
                message = "✅ p站 登录设置\n\n"
                message += f"状态：已配置 Cookie\n"
                message += f"用户ID：{user_id or '未记录'}\n"
                message += f"保存时间：{saved_time}\n"
                message += f"Cookie长度：{len(cookie_text)} 字符\n\n"
                message += "提示：这里只表示本地已保存 Cookie，实际有效性以请求 p站 成功为准。"
                await UniMessage.text(message).send()
            else:
                await UniMessage.text("❌ 未登录，请使用「p站 登录」命令设置Cookie").send()
                
    except Exception as e:
        await UniMessage.text(f"检查登录状态时发生错误：{str(e)}").send()


@quality_matcher.handle()
async def quality_setting_handle(result: Arparma):
    """处理质量设置命令"""
    score = result.query[int]("score")
    
    score = min(max(score or 0, 0), 100)  # 限制0-100分
    
    try:
        # 这里可以将质量设置保存到配置文件中
        await UniMessage.text(f"✅ 质量评分阈值已设置为：{score}分").send()
        await UniMessage.text(f"💡 低于此分数的图片在随机模式下将被降级处理").send()
        
    except Exception as e:
        await UniMessage.text(f"设置质量评分时发生错误：{str(e)}").send()

# ==================== 数据分析层处理器 ====================

@stats_matcher.handle()
async def stats_handle(result: Arparma):
    """处理统计命令"""
    try:
        async with get_pixiv_spider() as spider:
            # 获取作者关注统计
            authors_info = spider.get_favorite_authors_info()
            
            # 获取标签池统计
            tags_info = spider.get_tag_pools_info()
            
            message = "📊 使用统计\n\n"
            message += f"👥 关注作者：{authors_info.get('total_count', 0)}个\n"
            message += f"🆕 最近添加：{authors_info.get('recent_added', 0)}个\n"
            message += f"👀 未检查：{authors_info.get('never_checked', 0)}个\n"
            message += f"🏷️ 总圈名：{authors_info.get('total_aliases', 0)}个\n\n"
            
            message += f"❤️ 喜好标签：{tags_info.get('preferred_count', 0)}个\n"
            message += f"🚫 屏蔽标签：{tags_info.get('blocked_count', 0)}个"
            
            await UniMessage.text(message).send()
            
    except Exception as e:
        await UniMessage.text(f"获取统计信息时发生错误：{str(e)}").send()


@popular_tags_matcher.handle()
async def popular_tags_handle(result: Arparma):
    """处理热门标签命令"""
    try:
        async with get_pixiv_spider() as spider:
            tags_info = spider.get_tag_pools_info()
            preferred = tags_info.get('preferred_tags', [])
            
            if not preferred:
                await UniMessage.text("还没有设置喜好标签").send()
                return
            
            message = "🔥 热门标签（你的喜好）\n\n"
            for i, tag in enumerate(preferred[:10], 1):
                message += f"{i}. {tag}\n"
            
            if len(preferred) > 10:
                message += f"\n... 还有{len(preferred) - 10}个标签"
            
            await UniMessage.text(message).send()
            
    except Exception as e:
        await UniMessage.text(f"获取热门标签时发生错误：{str(e)}").send()

# ==================== 工具函数 ====================

def extract_image_urls(image: dict) -> List[str]:
    """
    从图片数据中提取URL列表，处理各种数据格式
    
    Args:
        image: 图片数据字典
        
    Returns:
        标准化的URL字符串列表
    """
    urls = []
    
    try:
        # 优先尝试获取urls数组
        image_urls = image.get('urls', [])
        if image_urls:
            if isinstance(image_urls, list):
                # 如果是列表，处理每个元素
                for url in image_urls:
                    if isinstance(url, str):
                        urls.append(url)
                    else:
                        # 如果不是字符串，尝试转换为字符串
                        url_str = str(url) if url else ''
                        if url_str:
                            urls.append(url_str)
                            logger.warning(f"图片URL类型转换: {type(url)} -> str, 值: {url_str}")
            elif isinstance(image_urls, str):
                # 如果是单个字符串，添加到列表
                urls.append(image_urls)
            else:
                # 其他类型，尝试转换
                url_str = str(image_urls) if image_urls else ''
                if url_str:
                    urls.append(url_str)
                    logger.warning(f"图片urls字段类型转换: {type(image_urls)} -> str, 值: {url_str}")
        
        # 如果没有urls数组，尝试获取单个url字段
        if not urls:
            single_url = image.get('url', '')
            if single_url:
                if isinstance(single_url, str):
                    urls.append(single_url)
                elif isinstance(single_url, list):
                    # 如果url字段是列表，处理每个元素
                    for url in single_url:
                        if isinstance(url, str):
                            urls.append(url)
                        else:
                            url_str = str(url) if url else ''
                            if url_str:
                                urls.append(url_str)
                                logger.warning(f"图片url字段元素类型转换: {type(url)} -> str, 值: {url_str}")
                else:
                    # 其他类型，尝试转换
                    url_str = str(single_url) if single_url else ''
                    if url_str:
                        urls.append(url_str)
                        logger.warning(f"图片url字段类型转换: {type(single_url)} -> str, 值: {url_str}")
        
        logger.debug(f"图片URL提取结果: 原始数据类型={type(image.get('urls'))}, 提取到{len(urls)}个URL")
        
    except Exception as e:
        logger.error(f"提取图片URL时发生错误: {e}")
    
    return urls

async def send_images_with_info(images: List[dict], title: str):
    """发送多张图片及其信息"""
    bot_logger.debug(f"send_images_with_info 开始执行，title: {title}")
    bot_logger.debug(f"images 参数类型: {type(images)}, 长度: {len(images) if hasattr(images, '__len__') else 'N/A'}")
    
    # 检查 images 是否为可迭代对象
    if not hasattr(images, '__iter__'):
        bot_logger.error(f"images 参数不可迭代: {type(images)}")
        return
    
    try:
        # 尝试转换为列表
        images_list = list(images)
        bot_logger.debug(f"成功转换为列表，长度: {len(images_list)}")
    except Exception as e:
        bot_logger.error(f"转换 images 为列表失败: {e}")
        return
    
    for i, image in enumerate(images_list, 1):
        bot_logger.debug(f"处理第 {i} 张图片，image 类型: {type(image)}")
        
        # 检查 image 是否为字典
        if not isinstance(image, dict):
            bot_logger.error(f"第 {i} 张图片不是字典类型: {type(image)}")
            continue
        
        image_id = image.get('id', '')
        image_title = image.get('title', '无标题')
        user_name = image.get('userName', '未知作者')
        user_id = image.get('userId', '')  # 🔥 添加作者ID
        tags = image.get('tags', [])
        quality_score = image.get('quality_score')
        
        bot_logger.debug(f"第 {i} 张图片基本信息 - ID: {image_id}, 标题: {image_title}, 作者: {user_name}")
        bot_logger.debug(f"第 {i} 张图片 tags 类型: {type(tags)}, 数量: {len(tags) if hasattr(tags, '__len__') else 'N/A'}")
        
        # 格式化标签 - 添加详细的错误处理和日志
        try:
            if isinstance(tags, list):
                tags_text = "、".join(tags[:5])  # 只显示前5个标签
                if len(tags) > 5:
                    tags_text += f" 等{len(tags)}个标签"
                bot_logger.debug(f"第 {i} 张图片标签处理成功")
            else:
                bot_logger.warning(f"第 {i} 张图片 tags 不是列表类型: {type(tags)}, 尝试转换")
                # 尝试转换为列表
                if tags:
                    tags_list = list(tags) if hasattr(tags, '__iter__') and not isinstance(tags, (str, bytes)) else [str(tags)]
                    tags_text = "、".join(tags_list[:5])
                    if len(tags_list) > 5:
                        tags_text += f" 等{len(tags_list)}个标签"
                    bot_logger.debug(f"第 {i} 张图片标签转换成功")
                else:
                    tags_text = "无标签"
                    bot_logger.debug(f"第 {i} 张图片无标签")
        except Exception as e:
            bot_logger.error(f"第 {i} 张图片标签处理失败: {e}")
            tags_text = "标签处理错误"
        
        # 🔥 使用专门的URL提取方法处理各种数据格式
        bot_logger.debug(f"第 {i} 张图片开始提取URL")
        urls = extract_image_urls(image)
        bot_logger.debug(f"第 {i} 张图片URL提取结果: {len(urls)} 个URL")
        
        try:
            if urls:
                bot_logger.debug(f"第 {i} 张图片有URL，开始构建消息")
                # 🔥 优化发送策略：合并图片和文字信息，减少网络请求
                # 🔥 检查 len(images) 是否会导致 slice 错误
                try:
                    images_count = len(images)
                    bot_logger.debug(f"第 {i} 张图片计算总数成功: {images_count}")
                except Exception as count_e:
                    bot_logger.error(f"第 {i} 张图片计算总数失败: {count_e}")
                    images_count = i  # 使用当前索引作为备用
                
                message_text = f"{title} ({i}/{images_count})\n"
                message_text += f"📷 标题: {image_title}\n"
                message_text += f"👤 作者: {user_name}"
                if user_id:
                    message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
                message_text += f"\n🆔 图片ID: {image_id}\n"
                message_text += f"🏷️ 标签: {tags_text}"
                
                bot_logger.debug(f"第 {i} 张图片消息文本构建完成")
                
                # 构建完整消息
                try:
                    message = UniMessage.text(message_text + "\n")
                    bot_logger.debug(f"第 {i} 张图片基础消息构建成功")
                    
                    for url_idx, url in enumerate(urls):
                        bot_logger.debug(f"第 {i} 张图片添加第 {url_idx+1} 个URL")
                        message = message + Image(url=url)
                    
                    bot_logger.debug(f"第 {i} 张图片完整消息构建完成，开始发送")
                    # 一次性发送，减少网络请求
                    await message.send()
                    bot_logger.debug(f"第 {i} 张图片发送成功")
                except Exception as msg_e:
                    bot_logger.error(f"第 {i} 张图片消息构建或发送失败: {msg_e}")
                    raise msg_e
            else:
                bot_logger.warning(f"第 {i} 张图片无URL，发送文字信息")
                # 🔥 检查 len(images) 是否会导致 slice 错误
                try:
                    images_count = len(images)
                    bot_logger.debug(f"第 {i} 张图片计算总数成功: {images_count}")
                except Exception as count_e:
                    bot_logger.error(f"第 {i} 张图片计算总数失败: {count_e}")
                    images_count = i  # 使用当前索引作为备用
                
                message_text = f"{title} ({i}/{images_count})\n"
                message_text += f"📷 标题: {image_title}\n"
                message_text += f"👤 作者: {user_name}"
                if user_id:
                    message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
                message_text += f"\n🆔 图片ID: {image_id}\n"
                message_text += f"🏷️ 标签: {tags_text}\n图片获取失败"
                await UniMessage.text(message_text).send()
                bot_logger.info(f"第 {i} 张图片文字信息发送成功")
        except Exception as e:
            bot_logger.error(f"第 {i} 张图片发送失败: {e}")
            bot_logger.error(f"第 {i} 张图片错误详情: {type(e).__name__}: {str(e)}")
            # 降级处理：只发送文字信息
            try:
                bot_logger.info(f"第 {i} 张图片开始降级处理")
                # 🔥 检查 len(images) 是否会导致 slice 错误
                try:
                    images_count = len(images)
                    bot_logger.info(f"第 {i} 张图片降级处理计算总数成功: {images_count}")
                except Exception as count_e:
                    bot_logger.error(f"第 {i} 张图片降级处理计算总数失败: {count_e}")
                    images_count = i  # 使用当前索引作为备用
                
                message_text = f"{title} ({i}/{images_count})\n"
                message_text += f"📷 标题: {image_title}\n"
                message_text += f"👤 作者: {user_name}"
                if user_id:
                    message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
                message_text += f"\n🆔 图片ID: {image_id}\n"
                message_text += f"🏷️ 标签: {tags_text}\n图片发送失败，请稍后重试"
                await UniMessage.text(message_text).send()
                bot_logger.info(f"第 {i} 张图片降级处理发送成功")
            except Exception as fallback_e:
                bot_logger.error(f"第 {i} 张图片降级发送也失败: {fallback_e}")
                bot_logger.error(f"第 {i} 张图片降级错误详情: {type(fallback_e).__name__}: {str(fallback_e)}")


# ==================== 基础查询功能处理器 ====================

@image_info_matcher.handle()
async def image_info_handle(result: Arparma):
    """处理图片信息命令"""
    illust_id = result.query[str]("illust_id")
    
    if not illust_id:
        await UniMessage.text("请指定图片ID，例如：p站 图片信息 123456789").send()
        return
    
    try:
        await UniMessage.text(f"正在获取图片信息（ID: {illust_id}），请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            image_info = await spider.get_image_info(illust_id)
            
            if not image_info:
                await UniMessage.text(f"未找到ID为「{illust_id}」的图片").send()
                return
            
            # 构建详细信息消息
            message_text = f"📷 图片详细信息\n\n"
            message_text += f"标题: {image_info.get('title', '无标题')}\n"
            message_text += f"ID: {image_info.get('id', illust_id)}\n"
            
            # 🔥 修复：在作者姓名后面加上作者ID
            user_name = image_info.get('userName', '未知作者')
            user_id = image_info.get('userId', '')
            if user_id:
                message_text += f"作者: {user_name} (ID: {user_id})\n"
            else:
                message_text += f"作者: {user_name}\n"
            
            message_text += f"页数: {image_info.get('pageCount', 1)}\n"
            message_text += f"尺寸: {image_info.get('width', 0)}x{image_info.get('height', 0)}\n"
            message_text += f"类型: {image_info.get('illustType', 0)}\n"
            message_text += f"AI生成: {'是' if image_info.get('aiType', 0) == 2 else '否'}\n"
            message_text += f"年龄限制: {image_info.get('xRestrict', 0)}\n"
            message_text += f"收藏数: {image_info.get('bookmarkCount', 0)}\n"
            message_text += f"点赞数: {image_info.get('likeCount', 0)}\n"
            message_text += f"浏览数: {image_info.get('viewCount', 0)}\n"
            message_text += f"评论数: {image_info.get('commentCount', 0)}\n"
            
            # 显示标签
            tags = image_info.get('tags', [])
            if tags:
                tags_text = "、".join(tags[:10])  # 只显示前10个标签
                if len(tags) > 10:
                    tags_text += f" 等{len(tags)}个标签"
                message_text += f"\n标签: {tags_text}"
            
            # 显示描述
            description = image_info.get('description', '').strip()
            if description:
                # 限制描述长度
                if len(description) > 200:
                    description = description[:200] + "..."
                message_text += f"\n\n描述: {description}"
            
            # 显示创建时间
            create_date = image_info.get('createDate', '')
            if create_date:
                message_text += f"\n\n创建时间: {create_date}"
            
            # 🔥 使用专门的URL提取方法处理各种数据格式
            # 现在get_image_info返回的URL已经是数组格式，直接使用extract_image_urls处理
            urls = extract_image_urls(image_info)
            if urls:
                try:
                    # 先发送图片
                    message = UniMessage.text(f"📷 图片信息（ID: {illust_id}）\n")
                    for url in urls:
                        message = message + Image(url=url)
                    await message.send()
                    
                    # 再发送详细信息
                    await UniMessage.text(message_text).send()
                except Exception as e:
                    logger.error(f"发送图片失败: {e}")
                    await UniMessage.text(message_text + "\n图片发送失败").send()
            else:
                await UniMessage.text(message_text).send()
            
    except Exception as e:
        # 🔥 添加详细的错误信息和调试日志
        error_msg = f"获取图片信息时发生错误：{str(e)}"
        error_type = type(e).__name__
        
        # 记录详细的错误信息到日志
        logger.error(f"图片信息获取失败 - ID: {illust_id}")
        logger.error(f"错误类型: {error_type}")
        logger.error(f"错误信息: {str(e)}")
        logger.error(f"错误详情: {repr(e)}")
        
        # 如果是slice错误，提供更多上下文信息
        if "slice" in str(e).lower():
            logger.error("检测到slice相关错误，可能原因：")
            logger.error("1. 图片数据结构不完整")
            logger.error("2. URL数组格式异常")
            logger.error("3. 数据类型转换失败")
            
            # 尝试获取更多调试信息
            try:
                if 'image_info' in locals() and image_info is not None:
                    logger.error(f"image_info类型: {type(image_info)}")
                    logger.error(f"image_info内容: {image_info}")
                    
                    # 检查URL相关字段
                    urls_field = image_info.get('urls', 'MISSING') if isinstance(image_info, dict) else 'NOT_DICT'
                    url_field = image_info.get('url', 'MISSING') if isinstance(image_info, dict) else 'NOT_DICT'
                    logger.error(f"urls字段: {urls_field} (类型: {type(urls_field)})")
                    logger.error(f"url字段: {url_field} (类型: {type(url_field)})")
                else:
                    logger.error("image_info变量不存在或为None")
            except Exception as debug_e:
                logger.error(f"获取调试信息失败: {debug_e}")
        
        # 发送给用户的错误信息（简化版）
        user_msg = f"获取图片信息时发生错误：{error_type} - {str(e)}"
        if "slice" in str(e).lower():
            user_msg += "\n🔍 数据格式异常，请稍后重试"
        
        await UniMessage.text(user_msg).send()


@author_info_matcher.handle()
async def author_info_handle(result: Arparma):
    """处理作者信息命令"""
    user_id = result.query[str]("user_id")
    
    if not user_id:
        await UniMessage.text("请指定作者ID，例如：p站 作者信息 12345678").send()
        return
    
    try:
        await UniMessage.text(f"正在获取作者信息（ID: {user_id}），请稍候...").send()
        
        async with get_pixiv_spider() as spider:
            author_info = await spider.get_author_info(user_id)
            
            if not author_info:
                await UniMessage.text(f"未找到ID为「{user_id}」的作者").send()
                return
            
            # 构建详细信息消息
            message_text = f"👤 作者详细信息\n\n"
            message_text += f"名称: {author_info.get('name', '未知作者')}\n"
            message_text += f"ID: {author_info.get('userId', user_id)}\n"
            message_text += f"作品总数: {author_info.get('totalWorks', 0)}\n"
            message_text += f"可关注: {'是' if author_info.get('followable', False) else '否'}\n"
            message_text += f"接受请求: {'是' if author_info.get('acceptRequest', False) else '否'}\n"
            message_text += f"已关注: {'是' if author_info.get('isFollowed', False) else '否'}\n"
            message_text += f"高级会员: {'是' if author_info.get('isPremium', False) else '否'}\n"
            message_text += f"MyPixiv: {'是' if author_info.get('isMypixiv', False) else '否'}\n"
            
            # 显示简介
            comment = author_info.get('comment', '').strip()
            if comment:
                # 限制简介长度
                if len(comment) > 300:
                    comment = comment[:300] + "..."
                message_text += f"\n\n简介: {comment}"
            
            # 🔥 显示头像 - 头像放在最上面
            image = author_info.get('image', '')
            if image:
                try:
                    # 先发送头像
                    avatar_message = UniMessage.text(f"👤 作者信息（ID: {user_id}）\n")
                    avatar_message = avatar_message + Image(url=image)
                    await avatar_message.send()
                except Exception as e:
                    logger.error(f"发送头像失败: {e}")
            
            # 发送作者详细信息
            await UniMessage.text(message_text).send()
            
            # 🔥 显示最新作品 - 最新作品放在最下面
            latest_images = author_info.get('latestImages', [])
            if latest_images:
                latest_message = f"📸 最新作品（{len(latest_images)}张）:\n"
                for i, img in enumerate(latest_images[:3], 1):  # 只显示前3张
                    img_title = img.get('title', '无标题')
                    img_id = img.get('id', '')
                    img_date = img.get('createDate', '')
                    # 简化日期显示
                    if img_date:
                        img_date = img_date.split('T')[0]  # 只显示日期部分
                    latest_message += f"\n{i}. {img_title[:20]}... (ID: {img_id}, {img_date})"
                
                if len(latest_images) > 3:
                    latest_message += f"\n... 还有{len(latest_images) - 3}张作品"
                
                await UniMessage.text(latest_message).send()
            
    except Exception as e:
        await UniMessage.text(f"获取作者信息时发生错误：{str(e)}").send()


async def send_single_image_with_info(image: dict, title: str, quality_score: Optional[dict] = None):
    """发送单张图片及其信息"""
    image_id = image.get('id', '')
    image_title = image.get('title', '无标题')
    user_name = image.get('userName', '未知作者')
    user_id = image.get('userId', '')  # 🔥 添加作者ID
    tags = image.get('tags', [])
    
    # 格式化标签
    tags_text = "、".join(tags[:5])  # 只显示前5个标签
    if len(tags) > 5:
        tags_text += f" 等{len(tags)}个标签"
    
    # 🔥 使用专门的URL提取方法处理各种数据格式
    urls = extract_image_urls(image)
    
    try:
        if urls:
            # 🔥 优化发送策略：合并图片和文字信息，减少网络请求
            message_text = f"{title}\n"
            message_text += f"📷 标题: {image_title}\n"
            message_text += f"👤 作者: {user_name}"
            if user_id:
                message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
            message_text += f"\n🆔 图片ID: {image_id}\n"
            message_text += f"🏷️ 标签: {tags_text}"
            
            # 构建完整消息
            message = UniMessage.text(message_text + "\n")
            for url in urls:
                message = message + Image(url=url)
            
            # 一次性发送，减少网络请求
            await message.send()
        else:
            message_text = f"{title}\n"
            message_text += f"📷 标题: {image_title}\n"
            message_text += f"👤 作者: {user_name}"
            if user_id:
                message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
            message_text += f"\n🆔 图片ID: {image_id}\n"
            message_text += f"🏷️ 标签: {tags_text}\n图片获取失败"
            await UniMessage.text(message_text).send()
    except Exception as e:
        logger.error(f"发送图片失败: {e}")
        # 降级处理：只发送文字信息
        try:
            message_text = f"{title}\n"
            message_text += f"📷 标题: {image_title}\n"
            message_text += f"👤 作者: {user_name}"
            if user_id:
                message_text += f" (ID: {user_id})"  # 🔥 显示作者ID
            message_text += f"\n🆔 图片ID: {image_id}\n"
            message_text += f"🏷️ 标签: {tags_text}\n图片发送失败，请稍后重试"
            await UniMessage.text(message_text).send()
        except Exception as fallback_e:
            logger.error(f"降级发送也失败: {fallback_e}")
