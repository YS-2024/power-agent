"""LangChain embeddings through the configured 302.AI-compatible endpoint."""
import math
import os
from functools import lru_cache

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langsmith import tracing_context
from openai import APIError

from .model import base_url
from .cancellation import cancellable_client


class EmbeddingError(RuntimeError):
    pass


def create_embedding_client():
    return httpx.Client(timeout=45, follow_redirects=False)


class APIEmbeddings(Embeddings):
    def __init__(self):
        self.model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip()
        self.base_url = base_url(os.getenv("EMBEDDING_BASE_URL", "https://api.302.ai/v1").strip())
        self.key = os.getenv("EMBEDDING_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
        if not self.model or not 1 <= self.dimensions <= 2000:
            raise ValueError("请配置向量模型及 1～2000 之间的 EMBEDDING_DIMENSIONS。")
        # The fingerprint deliberately excludes credentials.
        self.fingerprint = f"{self.base_url}|{self.model}|{self.dimensions}|line-chunks-v1"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.key:
            raise EmbeddingError("未配置向量接口密钥，请检查 EMBEDDING_API_KEY 或 LLM_API_KEY。")
        try:
            with tracing_context(enabled=False), create_embedding_client() as client, cancellable_client(client):
                embedding = OpenAIEmbeddings(
                    model=self.model, api_key=self.key, base_url=self.base_url,
                    dimensions=self.dimensions, max_retries=0, request_timeout=45,
                    http_client=client, check_embedding_ctx_length=False, chunk_size=32,
                    model_kwargs={"encoding_format": "float"},
                )
                vectors = embedding.embed_documents(texts)
            return validate_vectors(vectors, len(texts), self.dimensions)
        except EmbeddingError:
            raise
        except (APIError, httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise EmbeddingError("向量服务调用失败，请检查网络、模型配置及服务商额度后重试。") from None

    @lru_cache(maxsize=128)
    def _query(self, text):
        return tuple(self.embed_documents([text])[0])

    def embed_query(self, text: str) -> list[float]:
        return list(self._query(text))


def validate_vectors(vectors, count, dimensions):
    if len(vectors) != count:
        raise EmbeddingError("向量服务返回的片段数量不一致，资料未写入。")
    clean = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingError("向量维度与数据库配置不一致，请检查 EMBEDDING_DIMENSIONS。")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values) or not any(values):
            raise EmbeddingError("向量服务返回无效数值，资料未写入。")
        clean.append(values)
    return clean
