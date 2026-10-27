from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


STOP = {"什么", "怎么", "如何", "为什么", "请问", "可以", "是否", "哪些", "一个", "一下", "介绍", "解释", "报告", "生成", "整理", "资料", "关于", "以及", "进行", "的区", "区别", "有什么"}
SYNONYMS = {
    "度电": "电能 kWh 千瓦时", "电量": "电能 kWh 千瓦时", "功率因数": "功率因数 功率因子 有功 视在 PF",
    "功率因子": "功率因数 有功 视在 PF", "无功": "无功 功率因数", "触电": "电气安全 带电",
    "检修": "检修 电气安全 停电", "千瓦": "kW 功率", "千瓦时": "kWh 电能",
}


def tokens(text: str) -> list[str]:
    text = text.lower()
    result = re.findall(r"[a-z][a-z0-9_/-]*|\d+(?:\.\d+)?", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(run[i:i + 2] for i in range(len(run) - 1) if run[i:i + 2] not in STOP)
    return result


def split_chunks(content: str) -> list[dict]:
    chunks, buffer, size, start = [], [], 0, 1
    for line_number, line in enumerate(content.splitlines(), 1):
        # Long lines are kept in bounded pieces with the original line number.
        pieces = [line[i:i + 800] for i in range(0, len(line), 800)] or [""]
        for piece in pieces:
            if buffer and size + len(piece) > 900:
                chunks.append({"text": "\n".join(buffer), "start_line": start, "end_line": end})
                buffer, size = [], 0
            if not buffer:
                start = line_number
            buffer.append(piece)
            size += len(piece) + 1
            end = line_number
    if buffer:
        chunks.append({"text": "\n".join(buffer), "start_line": start, "end_line": end})
    return chunks


def located_chunks(content: str, filename: str) -> list[dict]:
    if Path(filename).suffix.lower() not in {".pdf", ".docx"}:
        return split_chunks(content)
    pattern = re.compile(r"^\[((?:PDF 第 \d+ 页)|(?:DOCX 段落 \d+)|(?:DOCX 表格 \d+ 第 \d+ 行))\]$")
    chunks, lines, location, start = [], [], "", 1

    def flush():
        for chunk in split_chunks("\n".join(lines)):
            if chunk["text"].strip():
                chunks.append(dict(chunk, start_line=chunk["start_line"] + start - 1,
                                   end_line=chunk["end_line"] + start - 1, location=location))

    for number, line in enumerate(content.splitlines(), 1):
        match = pattern.fullmatch(line)
        if match:
            flush()
            lines, location, start = [], match[1], number + 1
        else:
            lines.append(line)
    flush()
    return chunks


class KnowledgeBase:
    backend = "SQLite（兼容模式）"

    @property
    def chunk_count(self):
        return len(self.chunks)

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                content TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                source_url TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self._reindex()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _reindex(self):
        self.chunks = []
        with self.connect() as db:
            for row in db.execute("SELECT * FROM documents ORDER BY created_at, id"):
                for chunk in located_chunks(row["content"], row["filename"]):
                    chunk.update({"document_id": row["id"], "title": row["title"], "source_url": row["source_url"], "kind": row["kind"]})
                    chunk["terms"] = Counter(tokens(row["title"] + " " + chunk["text"]))
                    self.chunks.append(chunk)
        self.df = Counter(term for chunk in self.chunks for term in chunk["terms"])
        self.avg_length = sum(sum(c["terms"].values()) for c in self.chunks) / max(len(self.chunks), 1)

    def add(self, filename: str, content: str, *, title: str = "", source_url: str = "", kind: str = "用户资料", parsed=False):
        if not isinstance(filename, str) or not isinstance(content, str):
            raise ValueError("文件名和正文必须是文本。")
        filename = filename.replace("\\", "/").split("/")[-1][:160]
        if Path(filename).suffix.lower() not in ({".md", ".txt", ".pdf", ".docx"} if parsed else {".md", ".txt"}):
            raise ValueError("支持 TXT、Markdown、PDF 和 DOCX；PDF / DOCX 请上传原始文件供服务端解析。")
        content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
        if not content or "\x00" in content:
            raise ValueError("文件为空，或不是有效的文本文件。")
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("单份资料不能超过 1 MB。")
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT id FROM documents WHERE digest=?", (digest,)).fetchone()
            if existing:
                return {"id": existing["id"], "duplicate": True}
            if db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= 100:
                raise ValueError("本地原型最多保存 100 份资料，请先移除不需要的资料。")
            doc_id = uuid.uuid4().hex
            heading = re.search(r"^#\s+(.+)$", content, re.M)
            title = (title or (heading.group(1) if heading else Path(filename).stem))[:120]
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)", (doc_id, title, filename, content, digest, source_url, kind, datetime.now(timezone.utc).isoformat()))
            db.commit()
            self._reindex()
        return {"id": doc_id, "duplicate": False}

    def list_documents(self):
        with self.lock, self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,title,filename,source_url,kind,created_at,length(content) AS characters FROM documents ORDER BY created_at DESC")]

    def get(self, doc_id: str):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if row is None:
                raise KeyError("资料不存在。")
            return dict(row)

    def delete(self, doc_id: str):
        with self.lock, self.connect() as db:
            cursor = db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            if not cursor.rowcount:
                raise KeyError("资料不存在。")
            db.commit()
            self._reindex()

    def search(self, query: str, limit: int = 5):
        expanded = query + " " + " ".join(value for key, value in SYNONYMS.items() if key in query)
        query_terms = set(tokens(expanded))
        if not query_terms:
            return []
        with self.lock:
            ranked = []
            n = len(self.chunks)
            for chunk in self.chunks:
                terms = chunk["terms"]
                matching = query_terms & terms.keys()
                if not matching:
                    continue
                length = sum(terms.values())
                score = sum(math.log(1 + (n - self.df[t] + .5) / (self.df[t] + .5)) * (terms[t] * 2.2) / (terms[t] + 1.2 * (.25 + .75 * length / max(self.avg_length, 1))) for t in matching)
                score *= 1 + min(len(matching) / max(len(query_terms), 1), 1)
                result = {key: value for key, value in chunk.items() if key != "terms"}
                result["score"] = round(score, 3)
                ranked.append(result)
            ranked.sort(key=lambda item: item["score"], reverse=True)
            # Scores are ranking scores, never confidence probabilities.
            return [dict(item, citation=f"S{i}") for i, item in enumerate(ranked[:limit], 1)]
