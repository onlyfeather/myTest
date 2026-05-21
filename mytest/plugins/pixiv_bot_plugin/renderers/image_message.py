import asyncio
import hashlib
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx
from nonebot import logger
from nonebot_plugin_alconna.uniseg import Image, UniMessage

from ..common import MAX_IMAGES_PER_MESSAGE
from ..config import get_data_path

bot_logger = logger


_URL_PRIORITY = (
    "original",
    "regular",
    "large",
    "medium",
    "small",
    "thumb",
    "thumbnail",
)


def _normalize_url_values(value: Any) -> List[str]:
    if not value:
        return []

    if isinstance(value, str):
        return [value] if value else []

    if isinstance(value, list):
        urls: List[str] = []
        for item in value:
            urls.extend(_normalize_url_values(item))
        return urls

    if isinstance(value, dict):
        urls = []
        seen_keys = set()
        for key in _URL_PRIORITY:
            if key in value:
                seen_keys.add(key)
                urls.extend(_normalize_url_values(value.get(key)))
        for key, item in value.items():
            if key not in seen_keys:
                urls.extend(_normalize_url_values(item))
        return urls

    url = str(value)
    if url:
        bot_logger.warning(f"图片URL类型转换: {type(value)} -> str, 值: {url}")
        return [url]
    return []


def extract_image_urls(image: dict) -> List[str]:
    if not isinstance(image, dict):
        return []

    for key in ("urls", "url", "originalUrl"):
        urls = _normalize_url_values(image.get(key))
        if urls:
            bot_logger.debug(
                f"图片URL提取结果: 字段={key}, 原始类型={type(image.get(key))}, 提取到{len(urls)}个URL"
            )
            return urls

    bot_logger.debug("图片URL提取结果: 未提取到URL")

    return []


def _image_name_from_url(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if not suffix:
        suffix = ".jpg"
        if "png" in content_type.lower():
            suffix = ".png"
        elif "gif" in content_type.lower():
            suffix = ".gif"
        elif "webp" in content_type.lower():
            suffix = ".webp"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"pixiv_{digest}{suffix}"


async def _download_image_for_send(url: str, retries: int = 5) -> Optional[Image]:
    cache_dir = get_data_path("image_cache").parent / "image_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.pixiv.net/",
    }

    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=45.0,
                follow_redirects=True,
                trust_env=True,
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if content_type and not content_type.lower().startswith("image/"):
                    bot_logger.warning(
                        f"图片下载返回非图片内容: url={url}, content-type={content_type}"
                    )
                    return None

                raw = response.content
                if not raw:
                    bot_logger.warning(f"图片下载内容为空: url={url}")
                    return None

                name = _image_name_from_url(url, content_type)
                cache_path = cache_dir / name
                cache_path.write_bytes(raw)
                return Image(raw=raw, name=name)
        except Exception as e:
            bot_logger.warning(
                f"图片下载失败({attempt}/{retries}): "
                f"url={url}, error={type(e).__name__}: {e}"
            )
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)

    return None


async def _build_send_image_segment(url: str) -> Optional[Image]:
    image = await _download_image_for_send(url)
    if image is None:
        bot_logger.warning(f"图片下载多次失败，跳过远程URL发送以避免 send_msg 超时: {url}")
    return image


def _format_tags(tags) -> str:
    try:
        if isinstance(tags, list):
            tags_text = "、".join(tags[:5])
            if len(tags) > 5:
                tags_text += f" 等{len(tags)}个标签"
            return tags_text or "无标签"
        if tags:
            tags_list = (
                list(tags)
                if hasattr(tags, "__iter__") and not isinstance(tags, (str, bytes))
                else [str(tags)]
            )
            tags_text = "、".join(tags_list[:5])
            if len(tags_list) > 5:
                tags_text += f" 等{len(tags_list)}个标签"
            return tags_text or "无标签"
        return "无标签"
    except Exception as e:
        bot_logger.error(f"标签处理失败: {e}")
        return "标签处理错误"


def _build_image_text(image: dict, title: str, index: int | None = None, total: int | None = None) -> str:
    image_id = image.get("id", "")
    image_title = image.get("title", "无标题")
    user_name = image.get("userName", "未知作者")
    user_id = image.get("userId", "")
    tags_text = _format_tags(image.get("tags", []))

    prefix = title
    if index is not None and total is not None:
        prefix = f"{title} ({index}/{total})"

    message_text = f"{prefix}\n"
    message_text += f"📷 标题: {image_title}\n"
    message_text += f"👤 作者: {user_name}"
    if user_id:
        message_text += f" (ID: {user_id})"
    message_text += f"\n🆔 图片ID: {image_id}\n"
    message_text += f"🏷️ 标签: {tags_text}"
    return message_text


async def send_images_with_info(images: List[dict], title: str):
    bot_logger.debug(f"send_images_with_info 开始执行，title: {title}")

    if not hasattr(images, "__iter__"):
        bot_logger.error(f"images 参数不可迭代: {type(images)}")
        return

    try:
        images_list = list(images)
    except Exception as e:
        bot_logger.error(f"转换 images 为列表失败: {e}")
        return

    for index, image in enumerate(images_list, 1):
        if not isinstance(image, dict):
            bot_logger.error(f"第 {index} 张图片不是字典类型: {type(image)}")
            continue

        urls = extract_image_urls(image)
        if len(urls) > MAX_IMAGES_PER_MESSAGE:
            bot_logger.info(
                f"第 {index} 张图片包含 {len(urls)} 个URL，"
                f"单次消息仅发送前 {MAX_IMAGES_PER_MESSAGE} 个"
            )
            urls = urls[:MAX_IMAGES_PER_MESSAGE]

        message_text = _build_image_text(image, title, index, len(images_list))

        try:
            if urls:
                message = UniMessage.text(message_text + "\n")
                added_images = 0
                skipped_images = 0

                for url in urls:
                    image_segment = await _build_send_image_segment(url)
                    if image_segment is None:
                        skipped_images += 1
                        continue
                    message = message + image_segment
                    added_images += 1

                if skipped_images:
                    message = message + UniMessage.text(
                        f"\n有 {skipped_images} 张图片下载失败，已跳过，避免发送超时。"
                    )

                if added_images == 0:
                    message = message + UniMessage.text("\n图片下载失败，请稍后重试。")

                await message.send()
            else:
                await UniMessage.text(message_text + "\n图片获取失败").send()
        except Exception as e:
            bot_logger.error(f"第 {index} 张图片发送失败: {type(e).__name__}: {e}")
            try:
                await UniMessage.text(message_text + "\n图片发送失败，请稍后重试").send()
            except Exception as fallback_e:
                bot_logger.error(f"第 {index} 张图片降级发送也失败: {fallback_e}")


async def send_single_image_with_info(
    image: dict,
    title: str,
    quality_score: Optional[dict] = None,
):
    if not isinstance(image, dict):
        return

    message_text = _build_image_text(image, title)
    urls = extract_image_urls(image)

    try:
        if urls:
            message = UniMessage.text(message_text + "\n")
            for url in urls[:MAX_IMAGES_PER_MESSAGE]:
                image_segment = await _build_send_image_segment(url)
                if image_segment is not None:
                    message = message + image_segment
            await message.send()
        else:
            await UniMessage.text(message_text + "\n图片获取失败").send()
    except Exception as e:
        logger.error(f"发送图片失败: {e}")
        try:
            await UniMessage.text(message_text + "\n图片发送失败，请稍后重试").send()
        except Exception as fallback_e:
            logger.error(f"降级发送也失败: {fallback_e}")
