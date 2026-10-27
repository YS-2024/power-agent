"""Expose the configured knowledge store as a LangChain retriever."""
from typing import Any
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

class KnowledgeRetriever(BaseRetriever):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kb: Any = Field(exclude=True, repr=False)
    k: int = 4

    def _get_relevant_documents(self, query: str, *, run_manager):
        results = self.kb.search(query, limit=self.k)
        if getattr(results, "notice", "") and not results:
            # Preserve a degraded-search notice even when there are no matches.
            raise RetrievalUnavailable(results.notice)
        docs = []
        for item in results:
            metadata = {key: value for key, value in item.items() if key != "text"}
            if getattr(results, "notice", ""):
                metadata["retrieval_notice"] = results.notice
            docs.append(Document(page_content=item["text"], metadata=metadata))
        return docs


class RetrievalUnavailable(RuntimeError):
    pass
