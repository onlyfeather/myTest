"""参考图预处理：校验、按比例缩放、补边。"""

from __future__ import annotations

import io

from .http import ServiceError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_INPUT_PIXELS = 40_000_000  # 防止超大图片（解压炸弹）占满内存


def _open(data: bytes):
    try:
        from PIL import Image
    except ImportError:
        raise ServiceError("服务器缺少 Pillow，无法处理参考图（pip install pillow）") from None
    if len(data) > MAX_INPUT_BYTES:
        raise ServiceError(f"参考图太大了，最多 {MAX_INPUT_BYTES // 1024 // 1024} MB")
    Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ServiceError("参考图无法识别，请发送 PNG / JPG / WebP 图片") from None
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)  # 动图只取第一帧
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "transparency" in img.info or img.mode in ("LA", "PA") else "RGB")
    return img


def _flatten(img, background=(0, 0, 0)):
    from PIL import Image

    if img.mode == "RGBA":
        canvas = Image.new("RGB", img.size, background)
        canvas.paste(img, mask=img.split()[3])
        return canvas
    return img


def letterbox(data: bytes, canvases: tuple[tuple[int, int], ...]) -> tuple[bytes, tuple[int, int]]:
    """选宽高比最接近的画布，等比缩放后居中，四周补黑边。返回 (PNG, 画布尺寸)。"""
    from PIL import Image

    img = _flatten(_open(data))
    ratio = img.width / img.height
    width, height = min(canvases, key=lambda c: abs(c[0] / c[1] - ratio))
    scale = min(width / img.width, height / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    resized = img.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue(), (width, height)


def cover(data: bytes, sizes: dict[str, tuple[int, int]]) -> tuple[bytes, str]:
    """选宽高比最接近的尺寸，等比缩放到铺满后裁掉多出的部分（图生图不能有黑边）。返回 (PNG, 尺寸名)。"""
    from PIL import Image

    img = _flatten(_open(data))
    ratio = img.width / img.height
    name, (width, height) = min(sizes.items(), key=lambda item: abs(item[1][0] / item[1][1] - ratio))
    scale = max(width / img.width, height / img.height)
    size = (max(width, round(img.width * scale)), max(height, round(img.height * scale)))
    resized = img.resize(size, Image.Resampling.LANCZOS)
    left, top = (size[0] - width) // 2, (size[1] - height) // 2
    out = io.BytesIO()
    resized.crop((left, top, left + width, top + height)).save(out, format="PNG")
    return out.getvalue(), name


def normalize(data: bytes, *, max_side: int = 2048) -> tuple[bytes, str]:
    """统一转成 PNG，长边超过 max_side 时等比缩小。返回 (图片, MIME)。"""
    from PIL import Image

    img = _open(data)
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), "image/png"
