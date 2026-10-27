"""Explicit LangGraph workflows; all state is private to a single invocation."""
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .retriever import KnowledgeRetriever, RetrievalUnavailable


class KnowledgeState(TypedDict, total=False):
    question: str
    previous_question: str
    report: bool
    query: str
    sources: list[dict]
    answer: str
    mode: str
    notice: str
    failed: bool
    trace: list[dict]
    result: dict


class ChatState(TypedDict, total=False):
    message: str
    history: list
    images: list
    messages: list[dict]
    result: dict


def build_knowledge_graph(agent, error_types, failure_notice):
    def prepare(state):
        question = state["question"]
        if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2000:
            raise ValueError("请输入 2～2000 字的问题或报告主题。")
        question = question.strip()
        query = question
        previous = state.get("previous_question", "")
        if re.search(r"它|这些|上述|刚才|继续|这方面|这个", question) and isinstance(previous, str):
            query = previous[:2000] + " " + question
        return {"question": question, "query": query, "mode": "extractive", "notice": "", "failed": False,
                "trace": [{"tool": "理解任务", "detail": "生成资料报告" if state.get("report") else "检索并回答问题"}]}

    def retrieve(state):
        notice = ""
        try:
            docs = KnowledgeRetriever(kb=agent.kb, k=6 if state.get("report") else 4).invoke(state["query"])
        except RetrievalUnavailable as exc:
            docs, notice = [], str(exc)
        sources = [dict(doc.metadata, text=doc.page_content) for doc in docs]
        notice = notice or next((s.get("retrieval_notice", "") for s in sources if s.get("retrieval_notice")), "")
        return {"sources": sources, "notice": notice, "trace": state["trace"] + [{"tool": "知识库检索", "detail": f"LangChain Retriever 找到 {len(sources)} 个相关片段" + (f"；{notice}" if notice else "")}]}

    def route_sources(state):
        if not state["sources"]:
            return "no_evidence"
        return "generate" if agent.model_enabled else "fallback"

    def no_evidence(state):
        return {"answer": "当前知识库没有找到相关依据，暂时无法回答这个问题。请导入相关资料，或使用更具体的设备名称、术语或文件关键词重新提问。"}

    def generate(state):
        try:
            return {"answer": agent._generate(state["query"], state["sources"], state.get("report", False)), "failed": False,
                    "trace": state["trace"] + [{"tool": "模型生成", "detail": "通过 LangChain ChatOpenAI 生成回答"}]}
        except error_types as exc:
            return {"failed": True, "notice": state["notice"] + failure_notice(exc)}

    def validate_citations(state):
        used = set(re.findall(r"\[S(\d+)\]", state["answer"]))
        allowed = {str(i) for i in range(1, len(state["sources"]) + 1)}
        if not used or not used <= allowed:
            return {"failed": True, "notice": state["notice"] + "模型引用缺失或包含不存在的来源，引用校验未通过，已返回本地资料摘录。",
                    "trace": state["trace"] + [{"tool": "引用校验", "detail": "编号校验未通过，转入本地摘录"}]}
        return {"mode": "model", "failed": False,
                "trace": state["trace"] + [{"tool": "引用校验", "detail": "引用编号有效；内容仍需核对原文"}]}

    def fallback(state):
        blocks = [f"### {item['title']} [{item['citation']}]\n\n" + "\n".join("> " + line for line in item["text"].splitlines()) for item in state["sources"]]
        return {"answer": "以下是知识库中的相关原文摘录，尚未生成综合结论。片段相关性不代表已完整回答问题。\n\n" + "\n\n".join(blocks),
                "mode": "extractive", "trace": state["trace"] + [{"tool": "本地摘录", "detail": "按相关性返回原文和行号"}]}

    def respond(state):
        result = {key: state[key] for key in ("question", "answer", "sources", "trace", "mode", "notice")}
        result.update(model=agent.model if state["mode"] == "model" else "", provider=agent.provider if state["mode"] == "model" else "", engine="LangChain + LangGraph")
        return {"result": result}

    def report(state):
        return {"result": agent._export_report(dict(state["result"]))}

    graph = StateGraph(KnowledgeState)
    for name, node in {"prepare": prepare, "retrieve": retrieve, "no_evidence": no_evidence, "generate": generate,
                       "validate_citations": validate_citations, "fallback": fallback, "respond": respond, "export_report": report}.items():
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "retrieve")
    graph.add_conditional_edges("retrieve", route_sources, {name: name for name in ("no_evidence", "generate", "fallback")})
    graph.add_edge("no_evidence", "respond")
    graph.add_conditional_edges("generate", lambda s: "fallback" if s["failed"] else "validate_citations", {name: name for name in ("fallback", "validate_citations")})
    graph.add_conditional_edges("validate_citations", lambda s: "fallback" if s["failed"] else "respond", {name: name for name in ("fallback", "respond")})
    graph.add_edge("fallback", "respond")
    graph.add_conditional_edges("respond", lambda s: "export_report" if s.get("report") else END, {"export_report": "export_report", END: END})
    graph.add_edge("export_report", END)
    return graph.compile(name="power_knowledge_workflow")


def build_chat_graph(agent, error_types, failure_notice):
    def prepare(state):
        return {"messages": agent._prepare_chat(state["message"], state.get("history"), state.get("images"))}

    def unavailable(state):
        return {"result": {"ok": False, "error": "日常聊天需要接入模型，请先在 .env 中配置模型服务并重启。"}}

    def generate(state):
        try:
            answer = agent._complete(state["messages"])
        except error_types as exc:
            return {"result": {"ok": False, "error": failure_notice(exc, fallback=False)}}
        return {"result": {"ok": True, "answer": answer, "model": agent.model, "provider": agent.provider, "engine": "LangChain + LangGraph"}}

    graph = StateGraph(ChatState)
    graph.add_node("prepare_messages", prepare)
    graph.add_node("generate_chat", generate)
    graph.add_node("model_unavailable", unavailable)
    graph.add_edge(START, "prepare_messages")
    graph.add_conditional_edges("prepare_messages", lambda s: "generate_chat" if agent.model_enabled else "model_unavailable",
                                {"generate_chat": "generate_chat", "model_unavailable": "model_unavailable"})
    graph.add_edge("generate_chat", END)
    graph.add_edge("model_unavailable", END)
    return graph.compile(name="multimodal_chat_workflow")
