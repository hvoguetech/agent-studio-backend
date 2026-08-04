"""RAG: embeddings, vector store (Chroma or pgvector), ingestion, Q&A, hybrid search."""

from ros.knowledge.embeddings import Embedder, resolve_embedder
from ros.knowledge.store import ChromaStore, Hit, make_store

__all__ = ["Embedder", "resolve_embedder", "ChromaStore", "Hit", "make_store"]
