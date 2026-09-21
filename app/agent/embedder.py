"""Embedder: OpenAI (text-embedding-3-small, dim 1536) + Fake determinístico.

ÚNICA FUENTE DE VERDAD DE LA DIMENSIÓN: `EMBED_DIM` (este módulo).
`app/models/knowledge.py` la importa para construir la columna pgvector, y
`validate_embed_dim()` la coteja contra la columna real en BD al arranque:
si alguien creó la tabla con otra dimensión (1536 vs 768), falla rápido con
mensaje claro en vez de romper el RAG en silencio.
"""
import hashlib
import os
import re

import httpx

from app.agent.ports import EmbedderPort

# Dimensión canónica del esqueleto. Producción comercial: 1536 (OpenAI
# text-embedding-3-small). Instalación local con Ollama/nomic-embed-text:
# 768 (exportar EMBED_DIM=768 ANTES de crear el esquema).
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))


async def validate_embed_dim(session) -> int:
    """Valida que `knowledge_chunks.embedding` tenga dimensión EMBED_DIM.

    Llamar al arranque de la app (ver app/main.py). Lanza RuntimeError con
    mensaje accionable si hay mismatch (p.ej. tabla creada con 768 y
    EMBED_DIM=1536, o viceversa). Devuelve la dimensión real de la columna.
    """
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a JOIN pg_class c "
                "ON a.attrelid = c.oid "
                "WHERE c.relname = 'knowledge_chunks' "
                "AND a.attname = 'embedding' AND NOT a.attisdropped"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError(
            "validate_embed_dim: no se encontró la columna "
            "knowledge_chunks.embedding (¿esquema sin crear?)."
        )
    m = re.search(r"vector\((\d+)\)", row)
    actual = int(m.group(1)) if m else None
    if actual != EMBED_DIM:
        raise RuntimeError(
            f"MISMATCH de dimensión de embeddings: EMBED_DIM={EMBED_DIM} pero "
            f"la columna knowledge_chunks.embedding es {row}. "
            "Ajusta EMBED_DIM al embedder real ANTES de crear el esquema "
            "(OpenAI text-embedding-3-small=1536, Ollama nomic-embed-text=768) "
            "o migra la columna."
        )
    return actual


class OpenAIEmbedder:
    """Embedder real vía OpenAI embeddings API (async, sin SDK pesado)."""

    dimension = 1536
    model = "text-embedding-3-small"

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com/v1"):
        # La key puede venir resuelta por tenant (SecretProvider) o del env
        # global. Si EMBED_DIM != 1536, el arranque falla claro (validate_embed_dim).
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY requerido para OpenAIEmbedder")
        if EMBED_DIM != 1536:
            raise RuntimeError(
                f"OpenAIEmbedder produce vectores de 1536 pero EMBED_DIM={EMBED_DIM} "
                "(la columna pgvector no coincide). Usa el embedder que corresponda "
                "a tu EMBED_DIM."
            )

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": text},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
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
    y el pipeline RAG sin consumir API. La dimensión es EMBED_DIM (la única
    fuente de verdad), así coincide siempre con la columna pgvector.
    """

    dimension = EMBED_DIM

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

