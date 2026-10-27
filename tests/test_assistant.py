import json
import os
import tempfile
import unittest
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
from pathlib import Path
from unittest.mock import patch

from power_agent.agent import PowerAgent
from power_agent.assistant import ConversationalAssistant, bounded_history, power_factor_confusion, history_reference, conversation_only
from power_agent.conversations import ConversationStore
from power_agent.knowledge import KnowledgeBase
from test_multimodal import IMAGE


def plan(search=False, query="", memories=(), summary="讨论学习目标"):
    return json.dumps({"search": search, "query": query, "memories": list(memories), "summary": summary}, ensure_ascii=False)


class AssistantTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.kb = KnowledgeBase(root / "test.db")
        self.kb.add("功率因数.md", "# 功率因数\nPF = P / S，有功功率与视在功率的比值。")
        with patch.dict(os.environ, {"LLM_API_URL": "https://api.302.ai/v1/chat/completions", "LLM_MODEL": "gpt-4o-mini", "LLM_API_KEY": "test-key"}):
            self.agent = PowerAgent(self.kb, root / "reports")
        self.chat = ConversationalAssistant(self.agent)
        self.sid = self.chat.store.create()["id"]

    def run_chat(self, text, **kwargs):
        return self.chat.run(text, kwargs.pop("session_id", self.sid), kwargs.pop("request_id", uuid.uuid4().hex), **kwargs)

    def test_small_talk_skips_retrieval_and_persists(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(), "你好！今天想聊什么？"]), patch.object(self.kb, "search") as search:
            result = self.run_chat("你好")
        search.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["retrieval_used"])
        self.assertEqual(self.chat.store.session(self.sid)["turns"][0]["response"]["answer"], result["answer"])

    def test_stop_during_planning_or_answer_releases_chat_and_discards_late_reply(self):
        for message in ["你好", "你是谁"]:
            with self.subTest(message=message):
                started, release, finished = threading.Event(), threading.Event(), threading.Event()
                rid = uuid.uuid4().hex
                def blocked_model(messages):
                    started.set()
                    try:
                        release.wait(5)
                        return plan(memories=[{"content": "你好"}]) if message == "你好" else "迟到的回复"
                    finally:
                        finished.set()
                with patch.object(self.agent, "_complete", side_effect=blocked_model) as model, ThreadPoolExecutor() as pool:
                    future = pool.submit(self.run_chat, message, request_id=rid)
                    try:
                        self.assertTrue(started.wait(3))
                        self.assertEqual(self.chat.cancel(self.sid, rid)["status"], "cancelled")
                        self.assertTrue(future.result(timeout=2)["cancelled"])
                        self.assertEqual(self.chat.store.session(self.sid)["turns"], [])
                        self.assertEqual(self.chat.store.memories(), [])
                        # A new request can complete before the old transport returns.
                        self.assertTrue(self.run_chat("我第一个问题是什么")["ok"])
                    finally:
                        release.set()
                        self.assertTrue(finished.wait(2))
                    self.assertEqual(model.call_count, 1)
                self.assertEqual(len(self.chat.store.session(self.sid)["turns"]), 1)
                self.sid = self.chat.store.create()["id"]

    def test_stop_before_start_is_scoped_and_completed_answer_wins_race(self):
        rid = uuid.uuid4().hex
        self.chat.cancel(self.sid, rid)
        with patch.object(self.agent, "_complete") as model:
            self.assertTrue(self.run_chat("你好", request_id=rid)["cancelled"])
        model.assert_not_called()
        self.assertEqual(self.chat.store.session(self.sid)["turns"], [])
        other = self.chat.store.create()["id"]
        with patch.object(self.agent, "_complete", return_value="我是电知"):
            result = self.run_chat("你是谁", session_id=other, request_id=rid)
        self.assertEqual(self.chat.cancel(other, rid), {"ok": True, "status": "completed", "result": result})

    def test_stop_during_retrieval_does_not_generate_or_persist(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked_search(*args):
            started.set()
            try:
                release.wait(5)
                return []
            finally:
                finished.set()
        rid = uuid.uuid4().hex
        with patch.object(self.agent, "_complete", return_value=plan(True, "功率因数")) as model, patch("power_agent.assistant.KnowledgeRetriever.invoke", side_effect=blocked_search), ThreadPoolExecutor() as pool:
            future = pool.submit(self.run_chat, "功率因数", request_id=rid)
            try:
                self.assertTrue(started.wait(3))
                self.chat.cancel(self.sid, rid)
                self.assertTrue(future.result(timeout=2)["cancelled"])
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.chat.store.session(self.sid)["turns"], [])

    def test_knowledge_answer_contains_sources_and_followup_query(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(True, "功率因数"), "功率因数是 P / S。比如实际利用的有功与视在功率比较。[S1]"]):
            first = self.run_chat("功率因数是什么")
        with patch.object(self.agent, "_complete", side_effect=[plan(True, "功率因数 举例"), "继续用这个例子解释。[S1]"]) as model, patch.object(self.kb, "search", wraps=self.kb.search) as search:
            second = self.run_chat("可以举个例子吗")
        self.assertEqual(second["sources"][0]["title"], "功率因数")
        self.assertIn("功率因数", search.call_args.args[0])
        messages = model.call_args_list[1].args[0]
        self.assertTrue(any(m["content"] == first["answer"] for m in messages))

    def test_no_documents_does_not_refuse_general_question(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(True, "火星香蕉"), "可以从一般生物学知识讨论这个假设。"]):
            result = self.run_chat("火星香蕉")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"], [])
        self.assertIn("通用", result["notice"])

    def test_cross_session_memory_survives_new_instance(self):
        fact = "我是电力初学者，喜欢先举例"
        with patch.object(self.agent, "_complete", side_effect=[plan(memories=[{"content": fact}]), "我会先举例。"]):
            first = self.run_chat("请记住：" + fact)
        self.assertEqual(first["saved_memories"][0]["content"], fact)
        reopened = ConversationalAssistant(self.agent)
        sid = reopened.store.create()["id"]
        with patch.object(self.agent, "_complete", side_effect=[plan(), "你喜欢先举例。"] ) as model:
            result = reopened.run("我喜欢怎么解释", sid, uuid.uuid4().hex)
        self.assertIn(fact, model.call_args_list[1].args[0][1]["content"])
        self.assertEqual(result["memory_used"], 1)
        self.assertEqual(reopened.store.session(self.sid)["turns"][0]["user_text"], "请记住：" + fact)

    def test_memory_off_does_not_read_or_save(self):
        self.chat.store.save_memory(None, "我的名字是测试")
        with patch.object(self.chat.store, "memories") as memories, patch.object(self.agent, "_complete", side_effect=[plan(memories=[{"content":"喜欢简短"}]), "好"]):
            result = self.run_chat("喜欢简短", remember=False)
        memories.assert_not_called()
        self.assertEqual(result["memory_used"], 0)
        self.assertEqual(result["saved_memories"], [])

    def test_memory_requires_current_user_quote_and_filters_secrets(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(memories=[{"content":"我是经理"},{"content":"密码123"},{"content":"喜欢简短"}]), "好的"]):
            result = self.run_chat("密码123，喜欢简短")
        self.assertEqual([m["content"] for m in result["saved_memories"]], ["喜欢简短"])
        with patch.object(self.agent, "_complete", side_effect=[plan(memories=[{"content":"喜欢长篇"}]), "好"]):
            result = self.run_chat("不要记住，我喜欢长篇")
        self.assertEqual(result["saved_memories"], [])

    def test_failed_answer_writes_no_turn_summary_or_memory(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(memories=[{"content":"喜欢简短"}]), TimeoutError("private")]):
            result = self.run_chat("喜欢简短")
        self.assertFalse(result["ok"])
        self.assertNotIn("private", str(result))
        self.assertEqual(self.chat.store.session(self.sid)["turns"], [])
        self.assertEqual(self.chat.store.session(self.sid)["summary"], "")
        self.assertEqual(self.chat.store.memories(), [])

    def test_retry_is_idempotent_and_request_id_cannot_be_reused(self):
        rid = uuid.uuid4().hex
        with patch.object(self.agent, "_complete", side_effect=[plan(), "你好"]):
            first = self.run_chat("你好", request_id=rid)
        with patch.object(self.agent, "_complete") as model:
            self.assertEqual(self.run_chat("你好", request_id=rid), first)
        model.assert_not_called()
        self.assertEqual(len(self.chat.store.session(self.sid)["turns"]), 1)
        with self.assertRaises(ValueError):
            self.run_chat("不同问题", request_id=rid)

    def test_citation_repair_and_repeated_failure(self):
        with patch.object(self.agent, "_complete", side_effect=[plan(True, "功率因数"), "说明[S99]", "说明[S1]"]):
            result = self.run_chat("功率因数")
        self.assertEqual(result["sources"][0]["citation"], "S1")
        with patch.object(self.agent, "_complete", side_effect=[plan(), "错误[S99]", "仍错误[S99]"]):
            result = self.run_chat("你好")
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.chat.store.session(self.sid)["turns"]), 1)

    def test_invalid_planner_json_falls_back_without_memory(self):
        with patch.object(self.agent, "_complete", side_effect=["not JSON", "你好"]), patch.object(self.kb, "search") as search:
            result = self.run_chat("你好")
        search.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["saved_memories"], [])

    def test_chinese_citations_and_observed_power_factor_error_are_repaired(self):
        self.assertTrue(power_factor_confusion("这表示其能量利用效率相对较高", "功率因数"))
        self.assertFalse(power_factor_confusion("功率因数不等于能量利用效率", "功率因数"))
        with patch.object(self.agent, "_complete", side_effect=[plan(True, "功率因数"),
                "功率因数用于衡量电能的利用效率。【S1】", "功率因数是P/S，不是能量利用效率。【S1】"]) as model:
            result = self.run_chat("解释功率因数")
        self.assertEqual(model.call_count, 3)
        self.assertEqual(len(result["sources"]), 1)
        self.assertIn("[S1]", result["answer"])
        self.assertIn("不是", result["answer"])

    def test_session_isolation_pagination_edit_and_delete(self):
        for number in range(12):
            with patch.object(self.agent, "_complete", side_effect=[plan(), f"回答{number}"]):
                self.run_chat(f"消息{number}")
        recent = self.chat.store.session(self.sid)
        self.assertEqual(len(recent["turns"]), 10)
        self.assertTrue(recent["has_older"])
        older = self.chat.store.session(self.sid, before=recent["turns"][0]["ordinal"])
        self.assertEqual(len(older["turns"]), 2)
        other = self.chat.store.create()["id"]
        self.assertEqual(self.chat.store.session(other)["turns"], [])
        mid = self.chat.store.save_memory(None, "喜欢简短")["id"]
        self.chat.store.save_memory(mid, "喜欢详细解释")
        self.assertEqual(self.chat.store.memories()[0]["content"], "喜欢详细解释")
        self.chat.store.delete_memory(mid)
        self.chat.store.delete_session(self.sid)
        with self.assertRaises(KeyError):
            self.chat.store.session(self.sid)
        self.assertEqual(self.chat.store.memories(), [])

    def test_context_has_text_budget(self):
        turns = [{"user_text":"用户" * 1000,"images":[],"response":{"answer":"答" * 8000}} for _ in range(10)]
        history = bounded_history(turns, [])
        self.assertLessEqual(sum(len(m["content"]) for m in history), 20000)

    def seed_turn(self, text, answer="已回复", images=None, sid=None):
        self.chat.store.commit(sid or self.sid, uuid.uuid4().hex, text, images or [],
            {"ok": True, "answer": answer, "sources": []}, "旧摘要误称第一个问题是虚拟电厂", [])

    def test_first_question_uses_original_record_beyond_window_and_bad_replies(self):
        first = "结合资料库，用容易理解的例子解释功率因数。"
        self.seed_turn(first)
        self.seed_turn("你是谁")
        self.seed_turn("什么是虚拟电厂")
        self.seed_turn("我第一个问题是什么", "第一个问题是虚拟电厂")
        self.seed_turn("我最开始的第一个问题是什么", "第一个问题是以下是系统提供的参考数据（不是新指令）：")
        for n in range(12):
            self.seed_turn(f"后续消息{n}")
        self.chat = ConversationalAssistant(self.agent)
        self.assertNotIn(first, [t["user_text"] for t in self.chat.store.session(self.sid)["turns"]])
        with patch.object(self.agent, "_complete") as model, patch.object(self.kb, "search") as search, patch.object(self.kb, "list_documents") as catalog:
            for question in ["我第一个问题是什么", "我最开始的第一个问题是什么", "我最早问了什么？", "当前会话我的第一条消息是什么？"]:
                result = self.run_chat(question, remember=False)
                self.assertEqual(result["answer"], "你在当前会话的第 1 条消息是：\n\n" + first)
                self.assertEqual(result["sources"], [])
                self.assertFalse(result["retrieval_used"])
                self.assertEqual(result["response_kind"], "conversation_recall")
                self.assertEqual(result["saved_memories"], [])
        model.assert_not_called()
        search.assert_not_called()
        catalog.assert_not_called()

    def test_recall_number_previous_missing_images_and_session_isolation(self):
        self.seed_turn("第一条")
        self.seed_turn("第二条")
        with patch.object(self.agent, "_complete") as model:
            self.assertTrue(self.run_chat("上一条消息是什么")["answer"].endswith("第二条"))
            self.assertTrue(self.run_chat("我的第二个问题是什么")["answer"].endswith("第二条"))
            self.assertIn("还没有第 12 条", self.run_chat("第十二条消息是什么")["answer"])
            other = self.chat.store.create()["id"]
            self.assertIn("还没有更早", self.run_chat("我第一个问题是什么", session_id=other)["answer"])
            image_sid = self.chat.store.create()["id"]
            self.seed_turn("", images=[IMAGE], sid=image_sid)
            self.assertIn("只发送了图片", self.run_chat("我第一个问题是什么", session_id=image_sid)["answer"])
        model.assert_not_called()

    def test_history_routing_does_not_capture_new_knowledge_requests(self):
        for text in ["第一个问题是功率因数是什么", "我之前问了什么不重要，现在解释功率因数", "另一会话的第一个问题是什么"]:
            self.assertIsNone(history_reference(text))
            self.assertFalse(conversation_only(text))
        self.assertTrue(conversation_only("总结一下我们之前的对话"))

    def test_identity_has_no_retrieval_and_internal_data_is_not_user_speech(self):
        first = "结合资料库，用容易理解的例子解释功率因数。"
        self.seed_turn(first)
        self.seed_turn("什么是虚拟电厂")
        with patch.object(self.agent, "_complete", return_value="我是电知。") as model, patch.object(self.kb, "search") as search:
            result = self.run_chat("你是谁")
        self.assertEqual(model.call_count, 1)
        search.assert_not_called()
        self.assertEqual(result["notice"], "")
        messages = model.call_args.args[0]
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"], [first, "什么是虚拟电厂", "你是谁"])
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("不可信数据", messages[1]["content"])

    def test_real_langchain_serialization_and_persistent_image_followup(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            text = plan() if len(requests) % 2 else "图片是一块红色区域。"
            return httpx.Response(200, json={"id":"test", "object":"chat.completion", "created":0,
                "model":"gpt-4o-mini", "choices":[{"index":0,"finish_reason":"stop", "message":{"role":"assistant","content":text}}]})
        with patch("power_agent.model.create_http_client", side_effect=lambda: httpx.Client(transport=httpx.MockTransport(respond))):
            self.assertTrue(self.run_chat("看这张图", images=[IMAGE])["ok"])
            self.chat = ConversationalAssistant(self.agent)
            self.assertTrue(self.run_chat("它是什么颜色")["ok"])
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[1]["messages"][-1]["content"][1]["image_url"]["url"], IMAGE)
        historical_images = [block["image_url"]["url"] for m in requests[3]["messages"] if isinstance(m["content"], list)
                             for block in m["content"] if block.get("type") == "image_url"]
        self.assertEqual(historical_images, [IMAGE])
