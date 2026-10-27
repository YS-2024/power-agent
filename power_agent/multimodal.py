"""Bounded image inputs for the Chat Completions vision protocol."""
import base64
import binascii
import re

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_IMAGES = 8
MAX_CONTEXT_IMAGE_CHARS = 16_000_000


def validate_images(images):
    if not isinstance(images, list) or len(images) > 4:
        raise ValueError("每条消息最多附带 4 张图片。")
    result = []
    for value in images:
        if not isinstance(value, str) or len(value) > 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64:
            raise ValueError("单张处理后的图片不能超过 2 MB。")
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", value)
        if not match:
            raise ValueError("图片必须为 PNG、JPEG 或 WebP 的 Base64 数据，不接受外部链接。")
        try:
            raw = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片编码不正确，请重新选择图片。") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张处理后的图片不能超过 2 MB。")
        # Browser decodes/re-encodes files; these signatures check declared type,
        # not full integrity. The model provider also validates image decoding.
        valid = {
            "png": len(raw) >= 24 and raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[12:16] == b"IHDR",
            "jpeg": len(raw) >= 4 and raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"),
            "webp": len(raw) >= 16 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
        }[match[1]]
        if not valid:
            raise ValueError("图片内容与声明格式不符，请重新选择有效图片。")
        result.append(value)
    return result


def user_content(text, images):
    if not images:
        return text
    return [{"type": "text", "text": text or "请描述这些图片中的主要内容。"}, *[
        {"type": "image_url", "image_url": {"url": image, "detail": "auto"}}
        for image in images
    ]]
