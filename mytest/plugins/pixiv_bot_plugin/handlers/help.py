from nonebot_plugin_alconna import Alconna, Arparma, on_alconna
from nonebot_plugin_alconna.uniseg import UniMessage

from ..common import pixiv_command


help_cmd = Alconna(pixiv_command("帮助"))
help_matcher = on_alconna(help_cmd, use_cmd_start=True, block=True)


@help_matcher.handle()
async def help_handle(result: Arparma):
    help_text = """
🎨 Pixiv智能图片搜索机器人帮助

📸 核心功能 - 图片搜索与推荐：
• p站 搜图 [标签] [数量] [模式] - 智能搜图
• 搜图 [标签] [数量] [模式] - 核心短指令
  └─ 作品ID直达：p站 搜图 123456789
  └─ 随机模式默认要求收藏>100；手动收藏筛选低于100时仍按100执行
  └─ 模式选择：随机(random)、最新(recent)、热门(popular)、美图(beautiful)
  └─ 收藏筛选：可追加 收藏500、收藏>=1000、500收藏
• p站 最新 [标签] [数量] [小时] - 获取指定时间内收藏>50的最新图片
• p站 美图 [标签] - 获取1张高收藏精选美图（收藏>5000/2500/1000/500 回退）
• p站 热门 [标签] [数量] - 获取当前热门图片
• p站 每日一图 / 每日一图 - 获取个性化每日推荐

👥 作者追踪 - 作者管理与作品浏览：
• p站 作者 [ID/圈名] [数量] [模式] - 获取作者作品
  └─ 模式选择：随机(random)、最新(recent)、热门(popular)
• p站 作者信息 [ID/圈名] - 查看作者详细资料和统计数据
• p站 关注 [作者ID] [作者名] - 添加作者到关注列表
• p站 取关 [作者ID] - 从关注列表中移除作者
• p站 关注列表 - 查看已关注作者列表

🔍 基础查询 - 详细信息查询：
• p站 图片信息 [图片ID] - 获取图片完整信息和统计数据
• p站 作者信息 [ID/圈名] - 获取作者详细资料和最新作品

🎨 个性化配置 - 内容偏好管理：
• p站 偏好 [动作] [标签] - 管理内容偏好标签
  └─ 动作：查看、喜欢、不喜欢、屏蔽、不屏蔽
• p站 圈名 [动作] [参数] - 管理作者圈名别名
  └─ 动作：列表、设置、删除、搜索

🔧 系统管理 - 账户与设置：
• /p站 登录 [Cookie] - 设置Pixiv账户登录信息（仅 SUPERUSER 私聊）
• p站 登录状态 - 检查当前登录状态（仅 SUPERUSER 私聊）
• p站 质量设置 [分数] - 设置图片质量评分阈值(0-100，仅 SUPERUSER 私聊)

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


__all__ = ["help_matcher"]
