"""LangChain adapter for the configured OpenAI-compatible service."""
from urllib.parse import urlsplit, urlunsplit

import httpx
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from .cancellation import cancellable_client


class ModelError(ValueError):
    """Application-owned messages safe to display to the user."""


def base_url(api_url: str) -> str:
    url = urlsplit(api_url)
    if url.username or url.password or not url.hostname or url.query or url.fragment or (
        url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ModelError("模型地址必须为 HTTPS 或本机 HTTP，且不能包含凭据、查询参数或片段")
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def create_http_client():
    return httpx.Client(timeout=45, follow_redirects=False)


def complete(*, api_url, model, key, messages, max_tokens, temperature):
    endpoint = base_url(api_url)
    if urlsplit(endpoint).hostname == "api.302.ai" and not key:
        raise ModelError("302.AI 未配置 API 密钥，请填写本地 .env 中的 LLM_API_KEY")
    # No LangSmith upload, automatic retries or credential-bearing redirects.
    with tracing_context(enabled=False), create_http_client() as client, cancellable_client(client):
        llm = ChatOpenAI(
            model=model, api_key=key or "local-no-key", base_url=endpoint,
            temperature=temperature, max_tokens=max_tokens,
            timeout=45, max_retries=0, http_client=client,
            use_responses_api=False, stream_usage=False, http_socket_options=(),
        )
        response = llm.invoke(messages)
    reason = response.response_metadata.get("finish_reason")
    if reason == "length":
        raise ModelError("模型输出达到长度上限，请缩小问题范围或调高 LLM_MAX_TOKENS")
    if reason == "content_filter":
        raise ModelError("模型服务未提供正文，请调整问题后重试")
    answer = response.content
    if not isinstance(answer, str) or not answer.strip():
        raise ModelError("模型没有返回正文")
    if len(answer.encode("utf-8")) > 1_000_000:
        raise ModelError("模型响应过长")
    return answer.strip()
