"""输入预检：magic bytes 识别与文件名净化。

- Content-Length / 文件名均不可信；MIME 以 magic 为准、响应头仅供参考。
- 服务端生成路径与对象键；展示名只作清洗后的文件名。
"""

from __future__ import annotations

import re
import unicodedata

_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # docx/pptx/xlsx/zip 容器
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # 旧版 Office
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"RIFF", "application/octet-stream"),  # webp/wav/avi 需进一步区分
    (b"ID3", "audio/mpeg"),
    (b"\x1aE\xdf\xa3", "video/x-matroska"),
    (b"\x00\x00\x00", "video/mp4"),  # ftyp 在第 4-8 字节
]

_TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "csv", "json", "xml", "yaml", "yml", "log", "html", "htm"}
)

_SAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._ ()+\-\[\]]+")


def sanitize_display_filename(raw: str | None, *, default: str = "unnamed") -> str:
    """清洗展示文件名：去掉路径分隔与控制字符，限制长度。"""
    name = (raw or "").strip()
    if not name:
        return default
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip(". ")
    if not name:
        return default
    return name[:200]


def detect_mime(head: bytes, filename: str | None) -> str | None:
    """依据 magic bytes 与扩展名给出 MIME 提示；无法判定返回 None。"""
    if head.startswith(b"\x00\x00\x00") and len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"RIFF") and len(head) >= 12:
        if head[8:12] == b"WEBP":
            return "image/webp"
        if head[8:12] == b"WAVE":
            return "audio/wav"
        if head[8:12] == b"AVI ":
            return "video/x-msvideo"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\xff\xf1") or head.startswith(b"\xff\xf9"):
        return "audio/aac"
    for signature, mime in _MAGIC_SIGNATURES:
        if head.startswith(signature):
            return mime
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _TEXT_EXTENSIONS:
            return "text/plain"
    # 无 magic：尝试按文本解释（仅前 4KB 无 NUL 视为文本）
    if head and b"\x00" not in head[:4096]:
        try:
            head[:4096].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"
    return None


def is_zip_container(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06")


def looks_like_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF")
