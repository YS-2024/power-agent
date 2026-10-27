"""Exercise the real LangChain/OpenAI serialization without network requests."""
from contextlib import contextmanager
from unittest.mock import patch

import httpx


@contextmanager
def mock_model(answer="测试回答 [S1]", *, finish_reason="stop", status=200, handler=None):
    requests = []

    def respond(request):
        requests.append(request)
        if handler:
            return handler(request)
        body = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": finish_reason,
                             "message": {"role": "assistant", "content": answer}}]}
        if status != 200:
            body = {"error": {"message": "private-provider-detail", "type": "test_error"}}
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    with patch("power_agent.model.create_http_client", return_value=client):
        yield requests
    client.close()
