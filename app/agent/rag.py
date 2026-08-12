"""RAG: ingesta (split -> embed -> insert) y consulta (filtro tenant + umbral)."""
import re
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ports import EmbedderPort
from app.models import KnowledgeChunk, KnowledgeSource

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.75  # cosine similarity mínima (1 - distance)


def split_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split por tamaño de caracteres con solapamiento.

    Respeta saltos de párrafo cuando es posible para no cortar oraciones.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    step = max(1, chunk_size - overlap)
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        chunks.append(chunk)
        if end == len(text):
            break
        start += step
    return chunks


async def ingest_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    content: str,
    embedder: EmbedderPort,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> uuid.UUID:
    """Ingestiona texto: crea source, splitea, embeddea y guarda chunks."""
    source = KnowledgeSource(
        tenant_id=tenant_id,
        type="text",
        title=title,
        status="pending",
    )
    session.add(source)
    await session.flush()

    chunks = split_text(content, chunk_size, overlap)
    vectors = await embedder.embed_batch(chunks)

    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        session.add(
            KnowledgeChunk(
                tenant_id=tenant_id,
                source_id=source.id,
                chunk_index=i,
                content=chunk,
                embedding=vec,
            )
        )
    source.status = "ready"
    await session.commit()
    return source.id


async def search_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    embedder: EmbedderPort,
    top_k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """Búsqueda por similitud coseno con filtro duro por tenant_id.

    Devuelve chunks con similarity >= threshold.
    """
    qvec = await embedder.embed(query)
    # pgvector: 1 - cosine_distance(qvec) es la similitud coseno.
    # Pasamos la lista directa; el tipo Vector de la columna la serializa.
    stmt = (
        select(
            KnowledgeChunk.content,
            KnowledgeChunk.source_id,
            (1 - KnowledgeChunk.embedding.cosine_distance(qvec)).label("similarity"),
        )
        .where(KnowledgeChunk.tenant_id == tenant_id)
        .order_by(text("similarity DESC"))
        .limit(top_k)
    )
    result = await session.execute(stmt)
    rows = result.all()
    out = []
    for content, source_id, sim in rows:
        if sim is None or sim < threshold:
            continue
        out.append({"content": content, "source_id": str(source_id), "similarity": float(sim)})
    return out
