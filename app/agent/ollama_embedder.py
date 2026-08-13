"""Embedder local vía Ollama (gratis, sin API key).

Usa el modelo nomic-embed-text (o el que indique OLLAMA_EMBED_MODEL).
Produce embeddings semánticos reales para RAG sin dependencias de pago.

El dimension se infiere del primer embed; se expone para que el Vector type
de pgvector se configure al crear la tabla (en dev FakeEmbedder usa 1536 para
empatar con OpenAI; aquí nomic-embed-text es 768).
"""
import os

import httpx

from app.agent.ports import EmbedderPort


class OllamaEmbedder(EmbedderPort):
    """Embedder semántico local vía Ollama."""

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self._dim = int(os.getenv("OLLAMA_EMBED_DIM", "768"))

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.model, "prompt": text},
            )
            r.raise_for_status()
            return r.json()["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            out.append(await self.embed(t))
        return out
