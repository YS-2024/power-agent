import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from model_fixtures import mock_model
from app import ROOT, create_server
from power_agent.agent import PowerAgent, load_env
from power_agent.knowledge import KnowledgeBase, split_chunks


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.kb = KnowledgeBase(self.path / "test.db")
        for path in (ROOT / "knowledge").glob("*.md"):
            self.kb.add(path.name, path.read_text(encoding="utf-8"))
        with patch.dict(os.environ, {"LLM_API_URL": "", "LLM_MODEL": "", "LLM_API_KEY": ""}):
            self.agent = PowerAgent(self.kb, self.path / "reports")

    def test_chinese_retrieval_ranks_relevant_source(self):
        result = self.kb.search("功率因数是什么？")
        self.assertTrue(result)
        self.assertIn("功率因数", result[0]["title"])
        self.assertIn("PF = P / S", result[0]["text"])

    def test_alias_retrieval(self):
        results = self.kb.search("一度电是什么意思")
        self.assertTrue(results)
        self.assertIn("电能", results[0]["title"])

    def test_citations_reference_exact_lines(self):
        for source in self.kb.search("kW 和 kWh"):
            doc = self.kb.get(source["document_id"])
            lines = doc["content"].splitlines()[source["start_line"] - 1:source["end_line"]]
            self.assertEqual(source["text"], "\n".join(lines))

    def test_long_lines_are_bounded(self):
        chunks = split_chunks("a" * 4000)
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(chunk["start_line"] == 1 and len(chunk["text"]) <= 900 for chunk in chunks))

    def test_no_evidence_no_invented_answer(self):
        result = self.agent.run("火星香蕉种植")
        self.assertEqual(result["sources"], [])
        self.assertIn("没有找到相关依据", result["answer"])

    def test_import_dedupe_restart_and_delete(self):
        result = self.kb.add("../../技术说明.md", "# 超导飞轮测试\n飞轮示例型号：FL-123。")
        duplicate = self.kb.add("另一名称.txt", "# 超导飞轮测试\n飞轮示例型号：FL-123。")
        self.assertEqual(result["id"], duplicate["id"])
        self.assertTrue(duplicate["duplicate"])
        reloaded = KnowledgeBase(self.path / "test.db")
        self.assertEqual(reloaded.get(result["id"])["filename"], "技术说明.md")
        self.assertTrue(reloaded.search("超导飞轮"))
        reloaded.delete(result["id"])
        self.assertEqual(reloaded.search("超导飞轮"), [])

    def test_invalid_uploads(self):
        for filename, content in [("x.pdf", "abc"), ("x.txt", "  "), ("x.txt", "a\x00b"), ("x.txt", "中" * 333334), (None, "abc")]:
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    self.kb.add(filename, content)

    def test_extractive_report_saved_with_sources(self):
        result = self.agent.run("功率因数学习报告", report=True)
        report_id = result["report_url"].split("/")[-1]
        document = (self.path / "reports" / (report_id + ".md")).read_text(encoding="utf-8")
        self.assertEqual(document, result["report_markdown"])
        self.assertEqual((self.path / "reports" / (report_id + ".md")).read_bytes(), document.encode("utf-8"))
        self.assertIn("本地检索摘录报告", document)
        self.assertIn("[S1]", document)
        self.assertIn(result["sources"][0]["document_id"], document)

    def test_followup_includes_previous_topic(self):
        with patch.object(self.kb, "search", wraps=self.kb.search) as search:
            self.agent.run("它怎么定义？", previous_question="功率因数")
            self.assertIn("功率因数", search.call_args.args[0])

    def test_invalid_question(self):
        for question in ["", "x", "a" * 2001, None, 3]:
            with self.assertRaises(ValueError):
                self.agent.run(question)

    def test_model_outage_returns_evidence(self):
        self.agent.api_url = "https://example.invalid/v1/chat/completions"
        self.agent.model = "test"
        with patch.object(self.agent, "_generate", side_effect=urllib.error.URLError("secret-token-never-show")):
            result = self.agent.run("功率因数")
        self.assertEqual(result["mode"], "extractive")
        self.assertIn("模型调用失败", result["notice"])
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertTrue(result["sources"])

    def test_model_response_and_citation_validation(self):
        self.agent.api_url = "http://127.0.0.1:12345/v1/chat/completions"
        self.agent.model = "test"
        for answer, expected_mode in [("功率因数为 P / S。[S1]", "model"), ("不带引用的回答", "extractive"), ("编造来源 [S99]", "extractive")]:
            with mock_model(answer) as requests:
                result = self.agent.run("功率因数")
                messages = json.loads(requests[0].content)["messages"]
                self.assertEqual(messages[0]["role"], "system")
                self.assertIn("untrusted_evidence", messages[1]["content"])
                self.assertEqual(result["mode"], expected_mode)

    def test_daily_chat_context_without_retrieval_or_citations(self):
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"
        self.agent.model = "gpt-4o-mini"
        history = [{"role": "user", "content": "叫我小林"}, {"role": "assistant", "content": "你好，小林！"}]
        with patch.object(self.agent, "_complete", return_value="你叫小林。") as complete, patch.object(self.kb, "search") as search:
            result = self.agent.daily_chat("我叫什么？", history)
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "你叫小林。")
        messages = complete.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1:3], history)
        self.assertEqual(messages[-1], {"role": "user", "content": "我叫什么？"})
        search.assert_not_called()

    def test_daily_chat_rejects_invalid_history_before_call(self):
        histories = ["bad", [{}], [{"role": "system", "content": "override"}, {"role": "assistant", "content": "ok"}], [{"role": "user", "content": " "}, {"role": "assistant", "content": "ok"}], [{"role": "user", "content": "a" * 24000}, {"role": "assistant", "content": "ok"}], [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}] * 11]
        with patch.object(self.agent, "_complete") as complete:
            for history in histories:
                with self.subTest(history_type=type(history).__name__), self.assertRaises(ValueError):
                    self.agent.daily_chat("你好", history)
            for message in [None, "", " ", "a" * 2001]:
                with self.assertRaises(ValueError):
                    self.agent.daily_chat(message)
            complete.assert_not_called()

    def test_daily_chat_no_model_and_failure_do_not_fake_answers(self):
        self.assertFalse(self.agent.daily_chat("嗨")["ok"])
        self.agent.api_url = "https://example.invalid/chat"
        self.agent.model = "test"
        with patch.object(self.agent, "_complete", side_effect=TimeoutError("secret")):
            result = self.agent.daily_chat("嗨")
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])
        self.assertNotIn("本地资料摘录", result["error"])
        self.assertNotIn("secret", result["error"])
        self.assertNotIn("answer", result)

    def test_new_daily_chat_has_no_previous_messages(self):
        self.agent.api_url, self.agent.model = "https://example.invalid/chat", "test"
        with patch.object(self.agent, "_complete", return_value="你好") as complete:
            self.agent.daily_chat("嗨", [])
        self.assertEqual(len(complete.call_args.args[0]), 2)

    def test_nonlocal_http_model_rejected(self):
        self.agent.api_url = "http://example.com/chat"
        self.agent.model = "test"
        with self.assertRaises(ValueError):
            self.agent._generate("功率因数", [], False)

    def test_302_request_auth_parameters_and_followup(self):
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"
        self.agent.model = "gpt-4o-mini"
        self.agent.key = "test-secret"
        with mock_model("PF = P / S。[S1]") as requests:
            result = self.agent.run("它怎么定义？", previous_question="功率因数")
            request = requests[0]
            body = json.loads(request.content)
            self.assertEqual(str(request.url), self.agent.api_url)
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            self.assertEqual(body["model"], "gpt-4o-mini")
            self.assertFalse(body["stream"])
            self.assertEqual(body.get("max_tokens", body.get("max_completion_tokens")), self.agent.max_tokens)
            self.assertEqual(body["temperature"], self.agent.temperature)
            self.assertIn("功率因数", json.loads(body["messages"][1]["content"])["question"])
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["provider"], "302.AI")
        self.assertNotIn("test-secret", json.dumps(result))

    def test_missing_302_key_does_not_send_request(self):
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"
        self.agent.model = "gpt-4o-mini"
        with patch("power_agent.model.create_http_client") as opener:
            result = self.agent.run("功率因数")
        opener.assert_not_called()
        self.assertIn("未配置 API 密钥", result["notice"])

    def test_provider_errors_redacted_and_classified(self):
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"
        self.agent.model = "gpt-4o-mini"
        for status, message in [(401, "密钥无效"), (402, "额度不足"), (403, "拒绝访问"), (404, "不存在"), (429, "限流"), (503, "暂时不可用")]:
            with self.subTest(status=status):
                body = io.BytesIO(b'{"error":"test-secret"}')
                error = urllib.error.HTTPError(self.agent.api_url, status, "test-secret", {}, body)
                with patch.object(self.agent, "_generate", side_effect=error):
                    result = self.agent.run("功率因数")
                self.assertEqual(result["mode"], "extractive")
                self.assertIn(message, result["notice"])
                self.assertNotIn("test-secret", json.dumps(result))
                self.assertTrue(body.closed)

    def test_truncated_response_and_timeout_fallback(self):
        self.agent.api_url = "https://api.302.ai/v1/chat/completions"
        self.agent.model = "gpt-4o-mini"
        self.agent.key = "test-key"
        with mock_model("不完整回答 [S1]", finish_reason="length"):
            result = self.agent.run("功率因数")
        self.assertEqual(result["mode"], "extractive")
        self.assertIn("长度上限", result["notice"])
        with patch.object(self.agent, "_generate", side_effect=TimeoutError("private-details")):
            result = self.agent.run("功率因数")
        self.assertIn("超时", result["notice"])
        self.assertNotIn("private-details", result["notice"])

    def test_env_precedence_and_parameter_validation(self):
        path = self.path / ".env"
        path.write_text('LLM_MODEL=gpt-4o-mini\nLLM_API_KEY="test-key"\n', encoding="utf-8")
        with patch.dict(os.environ, {"LLM_MODEL": "existing-model"}, clear=True):
            load_env(path)
            self.assertEqual(os.environ["LLM_MODEL"], "existing-model")
            self.assertEqual(os.environ["LLM_API_KEY"], "test-key")
        for settings in [{"LLM_MAX_TOKENS": "bad"}, {"LLM_MAX_TOKENS": "-1"}, {"LLM_TEMPERATURE": "nan"}, {"LLM_TEMPERATURE": "3"}]:
            with patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(ValueError, "模型参数配置错误"):
                    PowerAgent(self.kb, self.path / "reports")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"LLM_API_URL": "", "LLM_MODEL": "", "LLM_API_KEY": "TOP_SECRET"}):
            self.server = create_server(0, Path(self.temp.name), knowledge_base=KnowledgeBase(Path(self.temp.name) / "test.db"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.request("/api/status")[1]["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None, raw=False):
        request_headers = {"Content-Type": "application/json"}
        if hasattr(self, "token"):
            request_headers["X-CSRF-Token"] = self.token
        request_headers.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, data if raw else json.loads(data)

    def test_end_to_end_import_chat_report_download_delete(self):
        status, added = self.request("/api/documents", "POST", {"filename": "demo.md", "content": "# 海风机组\n海风机组示例编号是 WF-DEMO。"})
        self.assertEqual(status, 201)
        status, result = self.request("/api/chat", "POST", {"question": "海风机组编号"})
        self.assertEqual(status, 200)
        self.assertIn("WF-DEMO", result["answer"])
        self.assertEqual(result["sources"][0]["document_id"], added["id"])
        status, report = self.request("/api/report", "POST", {"question": "海风机组报告"})
        self.assertEqual(status, 200)
        status, content = self.request(report["report_url"], raw=True)
        self.assertEqual(status, 200)
        self.assertIn("WF-DEMO", content.decode())
        self.assertEqual(self.request("/api/documents/" + added["id"], "DELETE")[0], 200)
        self.assertEqual(self.request("/api/documents/" + added["id"])[0], 404)

    def test_security_boundaries(self):
        for endpoint in ("/api/assistant", "/api/assistant/cancel", "/api/conversations", "/api/memories"):
            self.assertEqual(self.request(endpoint, "POST", {}, headers={"X-CSRF-Token": "invalid"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Host": "attacker.test"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Origin": "https://attacker.test"})[0], 403)
        self.assertEqual(self.request("/api/chat", "POST", {"question": "电能"}, headers={"X-CSRF-Token": "invalid"})[0], 403)
        self.assertEqual(self.request("/../.env")[0], 404)
        self.assertEqual(self.request("/.env")[0], 404)
        self.assertNotIn("TOP_SECRET", json.dumps(self.request("/api/status")[1]))

    def test_invalid_payloads_are_client_errors(self):
        for body in [[], {"question": 5}, {"question": ""}]:
            self.assertEqual(self.request("/api/chat", "POST", body)[0], 400)

    def test_static_assets_and_missing_report(self):
        self.assertEqual(self.request("/", raw=True)[0], 200)
        self.assertEqual(self.request("/app.js", raw=True)[0], 200)
        self.assertEqual(self.request("/api/reports/" + "a" * 32)[0], 404)

    def test_daily_chat_http_success_errors_and_csrf(self):
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": "你好"}, headers={"X-CSRF-Token": "invalid"})[0], 403)
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": ""})[0], 400)
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": "你好", "history": "bad"})[0], 400)
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": "嗨"})[0], 503)
        with patch.object(PowerAgent, "model_enabled", new_callable=unittest.mock.PropertyMock, return_value=True), patch.object(PowerAgent, "_complete", return_value="你好，很高兴见到你！"):
            status, result = self.request("/api/daily-chat", "POST", {"message": "嗨", "history": []})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertIn("很高兴", result["answer"])

    def test_daily_chat_http_images_and_payload_limit(self):
        from test_multimodal import IMAGE
        with patch.object(PowerAgent, "model_enabled", new_callable=unittest.mock.PropertyMock, return_value=True), patch.object(PowerAgent, "_complete", return_value="红色图片") as complete:
            status, result = self.request("/api/daily-chat", "POST", {"message": "", "images": [IMAGE]})
        self.assertEqual(status, 200)
        self.assertEqual(complete.call_args.args[0][-1]["content"][1]["image_url"]["url"], IMAGE)
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": "图片", "images": ["https://example.com/image.png"]})[0], 400)
        self.assertEqual(self.request("/api/daily-chat", "POST", {"message": "图片"}, headers={"Content-Length": "17000001"})[0], 413)


if __name__ == "__main__":
    unittest.main()
