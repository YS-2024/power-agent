import os

from .embeddings import APIEmbeddings
from .knowledge import KnowledgeBase
from .postgres import PostgresKnowledgeBase


def create_knowledge_store(data_dir):
    backend = os.getenv("KNOWLEDGE_BACKEND", "postgres").strip().lower()
    if backend == "sqlite":
        return KnowledgeBase(data_dir / "knowledge.db")
    if backend != "postgres":
        raise ValueError("KNOWLEDGE_BACKEND 仅支持 postgres 或 sqlite。")
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("请配置 DATABASE_URL 并启动 PostgreSQL；详见 docs/postgres.md。")
    return PostgresKnowledgeBase(dsn, APIEmbeddings(),
                                 schema=os.getenv("DATABASE_SCHEMA", "power_agent"),
                                 min_similarity=float(os.getenv("VECTOR_MIN_SIMILARITY", "0.3")))
