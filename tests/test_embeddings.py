import json
import os
import unittest
from unittest.mock import patch

import httpx

from power_agent.embeddings import APIEmbeddings, EmbeddingError, validate_vectors


class EmbeddingTests(unittest.TestCase):
    def model(self):
        with patch.dict(os.environ, {"EMBEDDING_BASE_URL": "https://api.302.ai/v1", "EMBEDDING_MODEL": "text-embedding-3-small",
                                    "EMBEDDING_DIMENSIONS": "3", "LLM_API_KEY": "test-secret", "EMBEDDING_API_KEY": ""}):
            return APIEmbeddings()

    def test_real_langchain_serialization_and_query_cache(self):
        requests = []
        def respond(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(body["encoding_format"], "float")
            self.assertEqual(body["input"], ["功率因数"])
            self.assertEqual(body["dimensions"], 3)
            self.assertEqual(str(request.url), "https://api.302.ai/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            return httpx.Response(200, json={"object": "list", "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [1., 0., 0.]}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2}})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch("power_agent.embeddings.create_embedding_client", return_value=client):
            model = self.model()
            self.assertEqual(model.embed_query("功率因数"), [1., 0., 0.])
            self.assertEqual(model.embed_query("功率因数"), [1., 0., 0.])
        self.assertEqual(len(requests), 1)

    def test_service_errors_are_safe_and_not_retried(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"message": "private-key-detail"}})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch("power_agent.embeddings.create_embedding_client", return_value=client):
            with self.assertRaises(EmbeddingError) as raised:
                self.model().embed_query("电能")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private-key", str(raised.exception))

    def test_reject_wrong_count_dimension_and_invalid_numbers(self):
        for vectors in [[], [[1, 0]], [[0, 0, 0]], [[float("nan"), 0, 1]], [[1, float("inf"), 0]]]:
            with self.subTest(vectors=vectors), self.assertRaises(EmbeddingError):
                validate_vectors(vectors, 1, 3)
