"""Bounded PDF/DOCX extraction in a disposable subprocess (no OCR)."""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

MAX_FILE_BYTES = 10_000_000
MAX_TEXT_BYTES = 1_000_000
MAX_PAGES = 200
_slots = threading.BoundedSemaphore(2)


def extract_document(data: bytes, suffix: str) -> dict:
    """Worker-only parsing; caller supplies a validated file type and byte limit."""
    sections, warnings, size = [], [], 0

    def append(location, text):
        nonlocal size
        # Normalize lines and prevent source text from impersonating location markers.
        text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
        text = re.sub(r"(?m)^\[(PDF|DOCX) ", r" [\1 ", text)
        if not text:
            return False
        section = f"[{location}]\n{text}"
        size += len(section.encode("utf-8")) + 2
        if size > MAX_TEXT_BYTES:
            raise ValueError("提取后的文本超过 1 MB，请拆分文档后导入。")
        if len(sections) >= 2000:
            raise ValueError("文档段落过多，请拆分文档后导入（最多 2000 个文本区段）。")
        sections.append(section)
        return True

    if suffix == ".pdf":
        from pypdf import PdfReader
        if not data.startswith(b"%PDF-"):
            raise ValueError("文件不是有效的 PDF。")
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF，请先解除密码保护后导入。")
        if len(reader.pages) > MAX_PAGES:
            raise ValueError("PDF 最多支持 200 页，请拆分后导入。")
        empty_pages = []
        for number, page in enumerate(reader.pages, 1):
            if not append(f"PDF 第 {number} 页", page.extract_text() or ""):
                empty_pages.append(number)
        if not sections:
            raise ValueError("PDF 没有可提取文字，可能是扫描件。请先进行 OCR 文字识别后再导入。")
        if empty_pages:
            warnings.append("以下 PDF 页未提取到文字，可能为空白页或扫描页：" + "、".join(map(str, empty_pages)) + "。扫描内容需要先进行 OCR。")
    elif suffix == ".docx":
        from docx import Document
        from docx.text.paragraph import Paragraph
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > 40_000_000:
                raise ValueError("DOCX 解压后过大，请拆分文档后导入。")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("文件不是有效的 DOCX。旧版 .doc 请另存为 .docx。")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("暂不支持加密 DOCX，请解除密码保护后导入。")
        doc = Document(io.BytesIO(data))
        paragraph_number = table_number = 0
        for block in doc.iter_inner_content():
            if isinstance(block, Paragraph):
                paragraph_number += 1
                append(f"DOCX 段落 {paragraph_number}", block.text)
            else:
                table_number += 1
                for row_number, row in enumerate(block.rows, 1):
                    # Merged cells may appear more than once in row.cells.
                    cells, seen = [], set()
                    for cell in row.cells:
                        if cell._tc not in seen:
                            seen.add(cell._tc)
                            cells.append(cell.text)
                    append(f"DOCX 表格 {table_number} 第 {row_number} 行", " | ".join(cells))
        if not sections:
            raise ValueError("DOCX 正文和表格中没有可提取文字。图片内容需要先进行 OCR。")
    else:
        raise ValueError("仅支持 PDF 和 DOCX 二进制文档。")
    return {"content": "\n\n".join(sections), "warnings": warnings}


def parse_upload(filename, encoded) -> dict:
    if not isinstance(filename, str):
        raise ValueError("文件名必须是文本。")
    filename = filename.replace("\\", "/").split("/")[-1]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("二进制上传仅支持 .pdf 和 .docx 文件。")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("PDF / DOCX 文件不能为空，且不能超过 10 MB。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("文件传输内容无效，请重新选择文件。") from None
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("PDF / DOCX 文件不能为空，且不能超过 10 MB。")
    if not _slots.acquire(blocking=False):
        raise ValueError("正在解析其他文档，请稍后重试。")
    try:
        with tempfile.TemporaryDirectory(prefix="power-agent-import-") as folder:
            path = Path(folder) / ("upload" + suffix)
            path.write_bytes(data)
            try:
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), str(path)],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                raise ValueError("文档解析超时，请缩小或重新导出文档后重试。") from None
            if result.returncode:
                raise ValueError("文档解析失败，请检查文件是否损坏或格式是否正确。")
            parsed = json.loads(result.stdout.decode("utf-8"))
            if "error" in parsed:
                raise ValueError(parsed["error"])
            return parsed
    finally:
        _slots.release()


if __name__ == "__main__":
    try:
        file = Path(sys.argv[1])
        output = extract_document(file.read_bytes(), file.suffix)
    except ValueError as exc:
        output = {"error": str(exc)}
    except Exception:
        output = {"error": "文档解析失败，请检查文件是否损坏、加密或格式是否正确。"}
    print(json.dumps(output, ensure_ascii=False))
