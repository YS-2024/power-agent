"""Real PostgreSQL integration tests, using isolated disposable schemas.

POSTGRES_TEST_URL must point at a development database with pgvector available.
No model API calls; embeddings are deterministic test fixtures.
"""
import os
import base64
import json
import threading
import urllib.request
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

from migrate_postgres import migrate
from app import create_server
from power_agent.agent import PowerAgent
from power_agent.embeddings import EmbeddingError
from power_agent.knowledge import KnowledgeBase
from power_agent.postgres import PostgresKnowledgeBase, StorageError
from document_fixtures import docx_bytes, pdf_bytes
from power_agent.assistant import ConversationalAssistant


class FakeEmbeddings:
    dimensions = 3
    fingerprint = "test-embeddings-v1|3"
    model = "fake"

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        if any(marker in text for marker in ("TABLE-773", "ZX-928")):
            return [0., 0., 1.]
        return [1., 0., 0.] if any(t in text for t in ("电", "PF", "P / S", "耗能")) else [0., 1., 0.]


@unittest.skipUnless(os.getenv("POSTGRES_TEST_URL"), "Set POSTGRES_TEST_URL for real database integration tests")
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["POSTGRES_TEST_URL"]
        self.schema = "test_power_" + uuid.uuid4().hex
        self.addCleanup(self.cleanup_schema)
        self.embeddings = FakeEmbeddings()
        self.kb = PostgresKnowledgeBase(self.dsn, self.embeddings, schema=self.schema, min_similarity=0.3)

    def cleanup_schema(self):
        # Only the random test schema created by this test may be removed.
        assert self.schema.startswith("test_power_") and len(self.schema) == 43
        with psycopg.connect(self.dsn) as db:
            db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def add(self):
        return self.kb.add("demo.md", "# 功率因数\nPF = P / S。", source_url="https://example.com/", kind="测试资料")

    def test_persistence_semantic_query_and_exact_citations(self):
        added = self.add()
        reopened = PostgresKnowledgeBase(self.dsn, self.embeddings, schema=self.schema)
        result = reopened.search("耗能")  # No lexical overlap, same fixture vector.
        self.assertEqual(result[0]["document_id"], added["id"])
        self.assertEqual(result.mode, "hybrid")
        self.assertGreater(result[0]["similarity"], .99)
        doc = reopened.get(added["id"])
        self.assertEqual(result[0]["text"], "\n".join(doc["content"].splitlines()[result[0]["start_line"]-1:result[0]["end_line"]]))
        self.assertEqual(result[0]["source_url"], "https://example.com/")

    def test_duplicate_skips_embedding_and_delete_cascades(self):
        added = self.add()
        with patch.object(self.embeddings, "embed_documents") as embed:
            duplicate = self.add()
        embed.assert_not_called()
        self.assertEqual(duplicate, {"id": added["id"], "duplicate": True})
        self.kb.delete(added["id"])
        self.assertEqual(self.kb.chunk_count, 0)
        self.assertEqual(self.kb.search("功率因数"), [])

    def test_demo_snapshot_has_vectors_but_no_private_chats_or_memories(self):
        from power_agent.sharing import snapshot_knowledge
        from power_agent.conversations import ConversationStore
        added = self.add()
        personal = ConversationStore(self.kb)
        private_sid = personal.create()['id']
        personal.save_memory(None, '个人空间私密记忆')
        schema = 'demo_' + uuid.uuid4().hex
        try:
            with patch.object(self.embeddings, 'embed_documents') as embed:
                demo = snapshot_knowledge(self.kb, schema)
            embed.assert_not_called()
            shared = ConversationStore(demo)
            self.assertEqual(shared.list_sessions(), [])
            self.assertEqual(shared.memories(), [])
            self.assertEqual(demo.get(added['id']), self.kb.get(added['id']))
            self.assertEqual(demo.chunk_count, self.kb.chunk_count)
            shared.create()
            shared.save_memory(None, '演示记忆')
            demo.delete(added['id'])
            self.assertEqual(personal.list_sessions()[0]['id'], private_sid)
            self.assertEqual(personal.memories()[0]['content'], '个人空间私密记忆')
            self.assertEqual(self.kb.chunk_count, 1)
            with self.assertRaises(ValueError):
                snapshot_knowledge(self.kb, schema)
        finally:
            assert schema.startswith('demo_') and len(schema) == 37
            with psycopg.connect(self.dsn) as db:
                db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    def test_invalid_embeddings_leave_no_partial_document(self):
        with patch.object(self.embeddings, "embed_documents", return_value=[[1., 0.]]):
            with self.assertRaises(EmbeddingError):
                self.add()
        self.assertEqual(self.kb.list_documents(), [])
        self.assertEqual(self.kb.chunk_count, 0)

    def test_database_error_rolls_back_document_and_earlier_chunks(self):
        with self.kb.connect() as db:
            db.execute("ALTER TABLE chunks ADD CHECK (text NOT LIKE '%ROLLBACK%')")
        with self.assertRaises(StorageError):
            self.kb.add("rollback.md", "电" * 900 + "\nROLLBACK" + "电" * 900)
        self.assertEqual(self.kb.list_documents(), [])
        self.assertEqual(self.kb.chunk_count, 0)

    def test_embedding_outage_falls_back_to_postgres_lexical_with_notice(self):
        self.add()
        with patch.object(self.embeddings, "embed_query", side_effect=EmbeddingError("private")):
            result = self.kb.search("功率因数")
            self.assertEqual(result.mode, "lexical")
            self.assertTrue(result)
            self.assertIn("仅使用", result.notice)
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LLM_API_URL": "", "LLM_MODEL": ""}):
                agent = PowerAgent(self.kb, Path(folder))
                self.assertIn("仅使用", agent.run("功率因数")["notice"])
                empty = agent.run("火星香蕉")
                self.assertEqual(empty["sources"], [])
                self.assertIn("仅使用", empty["notice"])

    def test_model_mismatch_prevents_mixing_indexes(self):
        other = FakeEmbeddings()
        other.fingerprint = "different-model|3"
        with self.assertRaisesRegex(StorageError, "不一致"):
            PostgresKnowledgeBase(self.dsn, other, schema=self.schema)

    def test_parallel_import_deduplicates_and_empty_query_does_not_call_api(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.add(), range(4)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        with patch.object(self.embeddings, "embed_query") as embed:
            self.assertEqual(self.kb.search(" "), [])
        embed.assert_not_called()

    def test_migration_preserves_ids_and_repeated_run_skips_paid_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = KnowledgeBase(root / "old.db")
            added = old.add("demo.md", "# 电力\n资料迁移测试", source_url="https://example.com", kind="测试")
            old_doc = old.get(added["id"])
            migrated = migrate(old.db_path, self.kb, root / "backups")
            self.assertEqual(migrated["added"], 1)
            self.assertTrue(Path(migrated["backup"]).is_file())
            self.assertEqual(self.kb.get(added["id"]), old_doc)
            self.assertTrue(self.kb.initialized)
            with patch.object(self.embeddings, "embed_documents") as embed:
                self.assertEqual(migrate(old.db_path, self.kb, root / "backups")["added"], 0)
            embed.assert_not_called()

    def test_chinese_and_model_number_keyword_search(self):
        added = self.kb.add("设备.md", "# 变压器设备\n型号 ABC-123/XY，试验资料。")
        with patch.object(self.embeddings, "embed_query", side_effect=EmbeddingError("offline")):
            result = self.kb.search("ABC-123/XY")
        self.assertEqual(result[0]["document_id"], added["id"])

    def test_conversation_memory_persistence_and_atomic_rollback(self):
        from power_agent.conversations import ConversationStore
        store = ConversationStore(self.kb)
        sid = store.create()["id"]
        result = {"ok": True, "answer": "测试回复", "sources": []}
        store.commit(sid, uuid.uuid4().hex, "喜欢简短解释", [], result, "讨论解释偏好", [{"content": "喜欢简短解释"}])
        reopened = ConversationStore(self.kb)
        self.assertEqual(reopened.session(sid)["turns"][0]["response"]["answer"], "测试回复")
        self.assertEqual(reopened.memories()[0]["content"], "喜欢简短解释")
        with self.kb.connect() as db:
            db.execute("ALTER TABLE conversation_turns ADD CHECK (user_text NOT LIKE '%ROLLBACK%')")
        with self.assertRaises(StorageError):
            store.commit(sid, uuid.uuid4().hex, "ROLLBACK 喜欢长篇", [], dict(result), "不应保存", [{"content": "喜欢长篇"}])
        self.assertEqual(len(reopened.memories()), 1)
        self.assertEqual(reopened.session(sid)["summary"], "讨论解释偏好")
        reopened.delete_session(sid)
        self.assertEqual(reopened.memories(), [])
        with reopened.kb.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM conversation_turns").fetchone()["n"], 0)

    def test_http_import_search_report_and_delete_use_postgres(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LLM_API_URL": "", "LLM_MODEL": ""}):
            self.kb.mark_initialized()
            server = create_server(0, Path(folder), knowledge_base=self.kb)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base + "/api/status") as response:
                    status = json.load(response)
                self.assertEqual(status["database"], "PostgreSQL + pgvector")
                def call(path, body=None, method="POST"):
                    request = urllib.request.Request(base + path, method=method,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={"Content-Type": "application/json", "X-CSRF-Token": status["csrf_token"]})
                    with urllib.request.urlopen(request) as response:
                        return json.load(response)
                doc = call("/api/documents", {"filename": "电力.md", "content": "# 电力测试\nPF = P / S。"})
                self.assertEqual(call("/api/search", {"question": "电力测试"})["retrieval"], "hybrid")
                report = call("/api/report", {"question": "电力测试"})
                self.assertIn("PF = P / S", report["report_markdown"])
                self.assertEqual(report["sources"][0]["document_id"], doc["id"])
                call("/api/documents/" + doc["id"], method="DELETE")
                self.assertEqual(self.kb.chunk_count, 0)
                for filename, data, query, location in [
                    ("test.pdf", pdf_bytes(), "ZX-928", "PDF 第 2 页"),
                    ("test.docx", docx_bytes(), "TABLE-773", "DOCX 表格 1 第 2 行"),
                ]:
                    body = {"filename": filename, "data_base64": base64.b64encode(data).decode()}
                    imported = call("/api/documents", body)
                    self.assertFalse(imported["duplicate"])
                    with patch.object(self.embeddings, "embed_documents") as embed:
                        self.assertTrue(call("/api/documents", body)["duplicate"])
                    embed.assert_not_called()
                    sources = call("/api/search", {"question": query})["sources"]
                    self.assertEqual(sources[0]["location"], location)
                    report = call("/api/report", {"question": query})
                    self.assertIn(location, report["report_markdown"])
                    self.assertIn(query, report["answer"])
                    call("/api/documents/" + imported["id"], method="DELETE")
                    self.assertEqual(self.kb.chunk_count, 0)
                for invalid in [{"filename": "bad.pdf", "content": "fake plain text"},
                                {"filename": "bad.docx", "data_base64": "YWJj"}]:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        call("/api/documents", invalid)
                    self.assertEqual(caught.exception.code, 400)
                    self.assertEqual(self.kb.list_documents(), [])
                self.assertFalse((Path(folder) / "knowledge.db").exists())
                session = call("/api/conversations", {})
                sid = session["id"]
                route = json.dumps({"search": False, "query": "", "summary": "用户喜欢简短", "memories": [{"content": "喜欢简短"}]})
                payload = {"session_id": sid, "request_id": uuid.uuid4().hex, "message": "请记住我喜欢简短"}
                with patch.object(PowerAgent, "model_enabled", new_callable=unittest.mock.PropertyMock, return_value=True), patch.object(PowerAgent, "_complete", side_effect=[route, "好的，我会简短解释。"]):
                    answer = call("/api/assistant", payload)
                self.assertTrue(answer["ok"])
                self.assertEqual(len(answer["saved_memories"]), 1)
                self.assertEqual(call("/api/assistant", payload)["answer"], answer["answer"])
                saved = call("/api/conversations/" + sid, method="GET")
                self.assertEqual(saved["turns"][0]["user_text"], payload["message"])
                # Cancel a real concurrent HTTP request before its model response returns.
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()
                stop_rid = uuid.uuid4().hex
                def blocked_model(messages):
                    entered.set()
                    try:
                        release.wait(5)
                        return "迟到的回答"
                    finally:
                        finished.set()
                with patch.object(PowerAgent, "model_enabled", new_callable=unittest.mock.PropertyMock, return_value=True), patch.object(PowerAgent, "_complete", side_effect=blocked_model), ThreadPoolExecutor() as pool:
                    pending = pool.submit(call, "/api/assistant", {"session_id": sid, "request_id": stop_rid, "message": "你是谁"})
                    try:
                        self.assertTrue(entered.wait(3))
                        stopped = call("/api/assistant/cancel", {"session_id": sid, "request_id": stop_rid})
                        self.assertEqual(stopped["status"], "cancelled")
                        self.assertTrue(pending.result(timeout=2)["cancelled"])
                    finally:
                        release.set()
                        self.assertTrue(finished.wait(2))
                self.assertEqual(len(call("/api/conversations/" + sid, method="GET")["turns"]), 1)
                # Exact recall must work without a model key, even beyond the recent window.
                store = ConversationalAssistant(PowerAgent(self.kb, Path(folder))).store
                for n in range(12):
                    store.commit(sid, uuid.uuid4().hex, f"后续消息{n}", [],
                        {"ok": True, "answer": "旧回复错误地说第一个问题是虚拟电厂", "sources": []}, "旧摘要", [])
                self.assertTrue(store.session(sid)["has_older"])
                self.assertEqual(store.user_turn(sid)["user_text"], payload["message"])
                self.assertEqual(store.user_turn(sid, -1)["ordinal"], 13)
                with patch.object(PowerAgent, "_complete") as model, patch.object(self.embeddings, "embed_query") as embed:
                    recalled = call("/api/assistant", {"session_id": sid, "request_id": uuid.uuid4().hex,
                        "message": "我最开始的第一个问题是什么", "remember": False})
                self.assertEqual(recalled["answer"], "你在当前会话的第 1 条消息是：\n\n" + payload["message"])
                self.assertEqual(recalled["sources"], [])
                model.assert_not_called()
                embed.assert_not_called()
                memory = call("/api/memories", method="GET")[0]
                call("/api/memories", {"id": memory["id"], "content": "喜欢详细解释"})
                self.assertEqual(call("/api/memories", method="GET")[0]["content"], "喜欢详细解释")
                call("/api/memories/" + memory["id"], method="DELETE")
                self.assertEqual(call("/api/memories", method="GET"), [])
                call("/api/conversations/" + sid, method="DELETE")
                self.assertEqual(call("/api/conversations", method="GET"), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
