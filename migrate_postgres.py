"""Copy the existing SQLite corpus to PostgreSQL; never delete the source.

Repeatable after interruption: matching content is skipped without embedding API
calls. IDs, source URLs, kinds and timestamps are retained for old report links.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from power_agent.agent import load_env
from power_agent.embeddings import EmbeddingError
from power_agent.postgres import PostgresKnowledgeBase, StorageError
from power_agent.storage import create_knowledge_store


ROOT = Path(__file__).resolve().parent


def migrate(source, target, backup_dir):
    if not source.is_file():
        raise ValueError("找不到待迁移的 SQLite 文件。")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ("knowledge-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        old.row_factory = sqlite3.Row
        with closing(sqlite3.connect(backup)) as snapshot:
            old.backup(snapshot)
        records = [dict(row) for row in old.execute("SELECT * FROM documents ORDER BY created_at,id")]
    migrated = []
    for doc in records:
        result = target.add(doc["filename"], doc["content"], title=doc["title"], source_url=doc["source_url"],
                            kind=doc["kind"], document_id=doc["id"], created_at=doc["created_at"],
                            parsed=Path(doc["filename"]).suffix.lower() in {".pdf", ".docx"})
        stored = target.get(result["id"])
        for field in ("id", "content", "digest", "title", "filename", "source_url", "kind", "created_at"):
            if stored[field] != doc[field]:
                raise ValueError("目标库中已有冲突资料，迁移校验未通过；原库保持不变。")
        migrated.append(result)
        print(f"已校验 {len(migrated)}/{len(records)}：{doc['title']}", flush=True)
    target.mark_initialized()
    return {"source": str(source), "backup": str(backup), "verified_documents": len(migrated),
            "added": sum(not item["duplicate"] for item in migrated), "chunks": target.chunk_count}


def main():
    parser = argparse.ArgumentParser(description="备份并迁移 SQLite 资料至 PostgreSQL（会调用向量 API）")
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "knowledge.db")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    try:
        target = create_knowledge_store(ROOT / "data")
        if not isinstance(target, PostgresKnowledgeBase):
            raise ValueError("迁移前请设置 KNOWLEDGE_BACKEND=postgres。")
        result = migrate(args.source.resolve(), target, ROOT / "data" / "backups")
        (ROOT / "data" / "postgres-migration.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"迁移完成：{result['verified_documents']} 份资料已校验，{result['chunks']} 个向量片段。")
    except (StorageError, EmbeddingError, ValueError) as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
