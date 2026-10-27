import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.documents import Document
from langsmith import tracing_context
from langsmith.run_helpers import get_tracing_context

from model_fixtures import mock_model
from power_agent.agent import PowerAgent
from power_agent.knowledge import KnowledgeBase
from power_agent.model import base_url
from power_agent.retriever import KnowledgeRetriever


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.kb = KnowledgeBase(root / "kb.db")
        self.kb.add("功率因数.md", "# 功率因数\nPF = P / S，功率因数是有功功率与视在功率的比值。")
        with patch.dict(os.environ, {"LLM_API_URL": "https://api.302.ai/v1/chat/completions",
                                    "LLM_MODEL": "gpt-4o-mini", "LLM_API_KEY": "test-key"}):
            self.agent = PowerAgent(self.kb, root / "reports")

    def steps(self, question="功率因数", report=False):
        with tracing_context(enabled=False):
            return list(self.agent.knowledge_graph.stream({"question": question, "report": report}, stream_mode="updates"))

    def test_retriever_keeps_exact_source_metadata(self):
        expected = self.kb.search("功率因数", limit=4)
        docs = KnowledgeRetriever(kb=self.kb).invoke("功率因数", config={"callbacks": []})
        self.assertIsInstance(docs[0], Document)
        self.assertEqual([dict(doc.metadata, text=doc.page_content) for doc in docs], expected)

    def test_successful_report_runs_generation_then_validation_then_export(self):
        with mock_model("功率因数为 P / S。[S1]") as requests:
            steps = self.steps(report=True)
        self.assertEqual([next(iter(step)) for step in steps],
                         ["prepare", "retrieve", "generate", "validate_citations", "respond", "export_report"])
        result = steps[-1]["export_report"]["result"]
        self.assertEqual(result["mode"], "model")
        self.assertIn("[S1]", result["report_markdown"])
        self.assertEqual(len(requests), 1)

    def test_invalid_citation_routes_to_fallback_before_export(self):
        with mock_model("无法验证的断言 [S99]"):
            steps = self.steps(report=True)
        self.assertEqual([next(iter(step)) for step in steps],
                         ["prepare", "retrieve", "generate", "validate_citations", "fallback", "respond", "export_report"])
        result = steps[-1]["export_report"]["result"]
        self.assertEqual(result["mode"], "extractive")
        self.assertNotIn("[S99]", result["report_markdown"])

    def test_no_evidence_and_offline_never_call_model(self):
        with patch.object(self.agent, "_complete") as complete:
            self.assertEqual([next(iter(step)) for step in self.steps("火星香蕉")],
                             ["prepare", "retrieve", "no_evidence", "respond"])
            self.agent.api_url = ""
            self.assertEqual([next(iter(step)) for step in self.steps()],
                             ["prepare", "retrieve", "fallback", "respond"])
        complete.assert_not_called()

    def test_sdk_errors_redacted_without_retries(self):
        for status, expected in [(401, "密钥无效"), (429, "限流"), (503, "暂时不可用")]:
            with self.subTest(status=status), mock_model(status=status) as requests:
                result = self.agent.run("功率因数")
            self.assertEqual(len(requests), 1)
            self.assertEqual(result["mode"], "extractive")
            self.assertIn(expected, result["notice"])
            self.assertNotIn("private-provider-detail", json.dumps(result))

    def test_sdk_timeout_and_redirect_do_not_retry_or_forward(self):
        def timeout(request):
            raise httpx.ReadTimeout("private-network-detail", request=request)

        with mock_model(handler=timeout) as requests:
            result = self.agent.daily_chat("你好")
        self.assertEqual(len(requests), 1)
        self.assertIn("超时", result["error"])
        self.assertNotIn("private-network-detail", str(result))
        with mock_model(handler=lambda r: httpx.Response(302, headers={"Location": "https://other.invalid/"})) as requests:
            result = self.agent.daily_chat("你好")
        self.assertEqual(len(requests), 1)
        self.assertFalse(result["ok"])

    def test_concurrent_calls_keep_state_and_chat_separate(self):
        self.agent.api_url = ""
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.agent.run, ["功率因数", "火星香蕉"] * 4))
        for index, result in enumerate(results):
            self.assertEqual(bool(result["sources"]), index % 2 == 0)
            self.assertEqual(len(result["trace"]), 3 if index % 2 == 0 else 2)
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"

        def reply(messages):
            self.assertEqual(len(messages), 2)
            return messages[-1]["content"]

        with patch.object(self.agent, "_complete", side_effect=reply), ThreadPoolExecutor(max_workers=4) as pool:
            chats = list(pool.map(self.agent.daily_chat, ["你好", "再见", "学习", "生活"]))
        self.assertEqual([chat["answer"] for chat in chats], ["你好", "再见", "学习", "生活"])

    def test_graph_does_not_inherit_external_tracing(self):
        def reply(messages):
            self.assertFalse(get_tracing_context()["enabled"])
            return "功率因数。[S1]"

        with tracing_context(enabled=True), patch.object(self.agent, "_complete", side_effect=reply):
            self.assertTrue(self.agent.daily_chat("你好")["ok"])
            self.assertEqual(self.agent.run("功率因数")["mode"], "model")

    def test_full_endpoint_and_base_endpoint_are_compatible(self):
        for endpoint in ["https://api.302.ai/v1", "https://api.302.ai/v1/chat/completions/"]:
            self.assertEqual(base_url(endpoint), "https://api.302.ai/v1")
        for endpoint in ["http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"]:
            with self.assertRaises(ValueError):
                base_url(endpoint)
