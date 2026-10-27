"""Transactional document store and PostgreSQL/pgvector hybrid retrieval.

LangChain's Embeddings and BaseRetriever interfaces remain the model/retrieval
boundary. Native SQL keeps document rows and vectors in one transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .embeddings import EmbeddingError, validate_vectors
from .knowledge import SYNONYMS, located_chunks, tokens


class StorageError(RuntimeError):
    pass


class SearchResults(list):
    def __init__(self, values=(), *, notice="", mode="hybrid"):
        super().__init__(values)
        self.notice, self.mode = notice, mode


class PostgresKnowledgeBase:
    backend = "PostgreSQL + pgvector"

    def __init__(self, dsn, embeddings, *, schema="power_agent", min_similarity=0.3):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("数据库 schema 名称无效。")
        if not math.isfinite(min_similarity) or not 0 <= min_similarity <= 1:
            raise ValueError("VECTOR_MIN_SIMILARITY 应为 0～1。")
        self.dsn, self.embeddings, self.schema = dsn, embeddings, schema
        self.dimensions = embeddings.dimensions
        if not isinstance(self.dimensions, int) or not 1 <= self.dimensions <= 2000:
            raise ValueError("向量维度应为 1～2000。")
        self.min_similarity = min_similarity
        self._initialize()

    @contextmanager
    def connect(self):
        try:
            with psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row) as db:
                db.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema)))
                db.execute("SET statement_timeout = '15s'")
                yield db
        except psycopg.Error:
            # Do not expose connection strings, passwords, SQL or provider data.
            raise StorageError("PostgreSQL 操作失败，请检查数据库服务、连接配置及 pgvector 扩展。") from None

    def _initialize(self):
        with self.connect() as db:
            db.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":schema",))
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            expected = self.embeddings.fingerprint
            existing = db.execute("SELECT value FROM settings WHERE key='embedding_fingerprint'").fetchone()
            if existing and existing["value"] != expected:
                raise StorageError("向量模型、接口或维度与已建索引不一致；请使用新 schema 重建索引后再切换，不能混用向量。")
            db.execute("INSERT INTO settings VALUES ('embedding_fingerprint', %s) ON CONFLICT DO NOTHING", (expected,))
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                content TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, source_url TEXT NOT NULL,
                kind TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute(f"""CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, text TEXT NOT NULL, start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL, terms TSVECTOR NOT NULL,
                embedding vector({self.dimensions}) NOT NULL,
                UNIQUE(document_id, ordinal))""")
            db.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id)")
            db.execute("CREATE INDEX IF NOT EXISTS chunks_terms_idx ON chunks USING gin(terms)")
            db.execute("CREATE INDEX IF NOT EXISTS chunks_vector_idx ON chunks USING hnsw(embedding vector_cosine_ops)")

    @property
    def chunk_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]

    @property
    def initialized(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone())

    def mark_initialized(self):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES ('initialized','1') ON CONFLICT DO NOTHING")

    def list_documents(self):
        with self.connect() as db:
            return db.execute("""SELECT id,title,filename,source_url,kind,created_at,
                length(content) AS characters FROM documents ORDER BY created_at DESC, id""").fetchall()

    def get(self, doc_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=%s", (doc_id,)).fetchone()
            if row is None:
                raise KeyError("资料不存在。")
            return row

    def delete(self, doc_id):
        with self.connect() as db:
            if not db.execute("DELETE FROM documents WHERE id=%s", (doc_id,)).rowcount:
                raise KeyError("资料不存在。")

    def add(self, filename, content, *, title="", source_url="", kind="用户资料", document_id=None, created_at=None, parsed=False):
        if not isinstance(filename, str) or not isinstance(content, str):
            raise ValueError("文件名和正文必须是文本。")
        filename = filename.replace("\\", "/").split("/")[-1][:160]
        if Path(filename).suffix.lower() not in ({".md", ".txt", ".pdf", ".docx"} if parsed else {".md", ".txt"}):
            raise ValueError("支持 TXT、Markdown、PDF 和 DOCX；PDF / DOCX 请上传原始文件供服务端解析。")
        content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
        if not content or "\x00" in content or len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("资料须为有效非空文本，且不能超过 1 MB。")
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM documents WHERE digest=%s", (digest,)).fetchone()
            if existing:
                return {"id": existing["id"], "duplicate": True}
            if db.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] >= 100:
                raise ValueError("本地原型最多保存 100 份资料。")
        doc_id = document_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", doc_id):
            raise ValueError("资料 ID 格式不正确。")
        heading = re.search(r"^#\s+(.+)$", content, re.M)
        title = (title or (heading.group(1) if heading else Path(filename).stem))[:120]
        chunks = located_chunks(content, filename)
        # Finish external API work before opening the write transaction.
        vectors = validate_vectors(self.embeddings.embed_documents([title + "\n" + c["text"] for c in chunks]), len(chunks), self.dimensions)
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":imports",))
            existing = db.execute("SELECT id FROM documents WHERE digest=%s", (digest,)).fetchone()
            if existing:
                return {"id": existing["id"], "duplicate": True}
            if db.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] >= 100:
                raise ValueError("本地原型最多保存 100 份资料。")
            db.execute("INSERT INTO documents VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                       (doc_id, title, filename, content, digest, source_url, kind,
                        created_at or datetime.now(timezone.utc).isoformat()))
            # Document and every chunk commit together; deletion cascades to vectors.
            for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors)):
                db.execute("""INSERT INTO chunks
                           (id,document_id,ordinal,text,start_line,end_line,terms,embedding,location)
                           VALUES (%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s::vector,%s)""",
                           (uuid.uuid4().hex, doc_id, ordinal, chunk["text"], chunk["start_line"], chunk["end_line"],
                            " ".join(tokens(title + " " + chunk["text"])), json.dumps(vector), chunk.get("location", "")))
        return {"id": doc_id, "duplicate": False}

    def search(self, query, limit=5):
        if not isinstance(query, str) or not query.strip():
            return SearchResults()
        limit = max(1, min(int(limit), 20))
        expanded = query + " " + " ".join(v for k, v in SYNONYMS.items() if k in query)
        terms = sorted(set(tokens(expanded)))
        candidate_limit = max(20, limit * 4)
        vector, notice = None, ""
        try:
            vector = validate_vectors([self.embeddings.embed_query(query)], 1, self.dimensions)[0]
        except EmbeddingError:
            notice = "向量服务暂不可用，本次仅使用 PostgreSQL 关键词检索。"
        columns = "c.id AS chunk_id,c.document_id,c.text,c.start_line,c.end_line,c.location,d.title,d.source_url,d.kind"
        with self.connect() as db:
            lexical = []
            if terms:
                tsquery = " | ".join("'" + term.replace("'", "''") + "'" for term in terms)
                lexical = db.execute(f"""SELECT {columns},ts_rank_cd(c.terms,q) AS lexical_score
                    FROM chunks c JOIN documents d ON d.id=c.document_id,
                    to_tsquery('simple', %s) q WHERE c.terms @@ q
                    ORDER BY lexical_score DESC,c.id LIMIT %s""", (tsquery, candidate_limit)).fetchall()
            semantic = []
            if vector is not None:
                serialized = json.dumps(vector)
                semantic = db.execute(f"""SELECT {columns},1-(c.embedding <=> %s::vector) AS similarity
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    ORDER BY c.embedding <=> %s::vector LIMIT %s""",
                                      (serialized, serialized, candidate_limit)).fetchall()
                semantic = [row for row in semantic if row["similarity"] >= self.min_similarity]
        # Reciprocal rank fusion: lexical and semantic scores have different units.
        merged = {}
        for rows in (lexical, semantic):
            for rank, row in enumerate(rows, 1):
                key = row["chunk_id"]
                if key not in merged:
                    merged[key] = dict(row, score=0.0)
                merged[key]["score"] += 1 / (60 + rank)
                if "similarity" in row:
                    merged[key]["similarity"] = row["similarity"]
        ranked = sorted(merged.values(), key=lambda r: (-r["score"], r["chunk_id"]))[:limit]
        return SearchResults([dict(row, score=round(row["score"], 6), citation=f"S{i}") for i, row in enumerate(ranked, 1)],
                             notice=notice, mode="lexical" if notice else "hybrid")
