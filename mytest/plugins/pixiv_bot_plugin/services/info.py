from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ImageInfoResult:
    ok: bool
    message: str
    image_info: Optional[Dict[str, Any]] = None


async def get_image_info(spider, illust_id: str) -> ImageInfoResult:
    if not illust_id:
        return ImageInfoResult(False, "请指定图片ID，例如：p站 图片信息 123456789")

    image_info = await spider.get_image_info(illust_id)
    if not image_info:
        return ImageInfoResult(False, f"未找到ID为「{illust_id}」的图片")
    return ImageInfoResult(True, "", image_info=image_info)


def build_image_info_message(image_info: Dict[str, Any], fallback_id: str) -> str:
    message = "📷 图片详细信息\n\n"
    message += f"标题: {image_info.get('title', '无标题')}\n"
    message += f"ID: {image_info.get('id', fallback_id)}\n"

    user_name = image_info.get("userName", "未知作者")
    user_id = image_info.get("userId", "")
    if user_id:
        message += f"作者: {user_name} (ID: {user_id})\n"
    else:
        message += f"作者: {user_name}\n"

    message += f"页数: {image_info.get('pageCount', 1)}\n"
    message += f"尺寸: {image_info.get('width', 0)}x{image_info.get('height', 0)}\n"
    message += f"类型: {image_info.get('illustType', 0)}\n"
    message += f"AI生成: {'是' if image_info.get('aiType', 0) == 2 else '否'}\n"
    message += f"年龄限制: {image_info.get('xRestrict', 0)}\n"
    message += f"收藏数: {image_info.get('bookmarkCount', 0)}\n"
    message += f"点赞数: {image_info.get('likeCount', 0)}\n"
    message += f"浏览数: {image_info.get('viewCount', 0)}\n"
    message += f"评论数: {image_info.get('commentCount', 0)}\n"

    tags = image_info.get("tags", [])
    if tags:
        tags_text = "、".join(tags[:10])
        if len(tags) > 10:
            tags_text += f" 等{len(tags)}个标签"
        message += f"\n标签: {tags_text}"

    description = image_info.get("description", "").strip()
    if description:
        if len(description) > 200:
            description = description[:200] + "..."
        message += f"\n\n描述: {description}"

    create_date = image_info.get("createDate", "")
    if create_date:
        message += f"\n\n创建时间: {create_date}"
    return message
