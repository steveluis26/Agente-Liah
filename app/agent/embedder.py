"""Embedder: OpenAI (text-embedding-3-small, dim 1536) + Fake determinístico.

El FakeEmbedder se usa en tests/smoke sin consumir API ni red.
"""
import hashlib
import os

import httpx

from app.agent.ports import EmbedderPort


class OpenAIEmbedder:
    """Embedder real vía OpenAI embeddings API (async, sin SDK pesado)."""

    dimension = 1536
    model = "text-embedding-3-small"

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY requerido para OpenAIEmbedder")

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": text},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]


class FakeEmbedder:
    """Embedder determinístico PARA TESTS (bag-of-words hasheado).

    NO semántico como OpenAI, pero dos textos con palabras en común obtienen
    mayor similitud coseno que textos disjuntos -> permite testear el ranking
    y el pipeline RAG sin consumir API. Dimensión 1536 para compatibilidad.
    """

    dimension = 1536

    async def embed(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text: str) -> list[float]:
        import re

        vec = [0.0] * FakeEmbedder.dimension
        # tokens normalizados
        tokens = re.findall(r"\w+", text.lower())
        if not tokens:
            return vec
        for tok in tokens:
            # reparte energía en N dimensiones derivadas del token
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            for k in range(4):
                idx = (h >> (k * 8)) % FakeEmbedder.dimension
                vec[idx] += 1.0
        # normaliza a vector unitario (coseno estable)
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

