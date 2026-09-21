"""RAG: ingesta (split -> embed -> insert) y consulta (filtro tenant + umbral)."""
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ports import EmbedderPort
from app.models import KnowledgeChunk, KnowledgeSource
from app.models.knowledge import EMBED_DIM

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.75  # cosine similarity mínima (1 - distance)
# Umbral ÚNICO del sistema: el engine y las tools usan este valor. No hay
# segundos umbrales escondidos (Fase 1 unifica 0.0 vs 0.75).
RAG_THRESHOLD = DEFAULT_THRESHOLD
INGEST_BATCH_SIZE = 64


def split_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split por párrafos empaquetados hasta `chunk_size` caracteres.

    Respeta saltos de párrafo: nunca corta a la mitad un párrafo salvo que un
    solo párrafo exceda `chunk_size` (ahí sí se corta duro, con solapamiento
    de `overlap` caracteres para no perder contexto en el borde).
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush():
        if current:
            chunks.append(" ".join(current))
            current.clear()

    for p in paragraphs:
        # Párrafo gigante: córtalo duro con solapamiento.
        while len(p) > chunk_size:
            _flush()
            chunks.append(p[:chunk_size])
            p = p[chunk_size - overlap:]
        if current_len + len(p) + (1 if current else 0) > chunk_size:
            _flush()
            current_len = 0
        current.append(p)
        current_len += len(p) + 1
    _flush()
    return chunks


def _check_dimension(embedder: EmbedderPort) -> None:
    """Valida que el embedder coincida con la columna pgvector.

    Un embedder con dimensión distinta (p.ej. 768 de Ollama vs 1536 de la
    columna) antes rompía pgvector en silencio o con errores crípticos; ahora
    falla explícito al ingerir/buscar.
    """
    if embedder.dimension != EMBED_DIM:
        raise ValueError(
            f"Dimensión del embedder ({embedder.dimension}) no coincide con "
            f"la columna pgvector ({EMBED_DIM}). Ajusta EMBED_DIM al embedder "
            "en uso o usa el embedder correcto para este despliegue."
        )


async def ingest_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    content: str,
    embedder: EmbedderPort,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> uuid.UUID:
    """Ingestiona texto: crea source, splitea, embeddea e inserta por lotes.

    Commits parciales por lote (no una sola transacción gigante). Si un lote
    falla, la fuente queda en `status="failed"` y se relanza la excepción.
    """
    _check_dimension(embedder)
    source = KnowledgeSource(
        tenant_id=tenant_id,
        type="text",
        title=title,
        status="pending",
    )
    session.add(source)
    await session.commit()  # la fuente existe aunque los lotes fallen
    source_id = source.id

    try:
        chunks = split_text(content, chunk_size, overlap)
        for batch_start in range(0, len(chunks), INGEST_BATCH_SIZE):
            batch = chunks[batch_start:batch_start + INGEST_BATCH_SIZE]
            vectors = await embedder.embed_batch(batch)
            for i, (chunk, vec) in enumerate(zip(batch, vectors)):
                if len(vec) != EMBED_DIM:
                    raise ValueError(
                        f"Vector de dimensión {len(vec)} != {EMBED_DIM} "
                        f"(chunk {batch_start + i})"
                    )
                session.add(
                    KnowledgeChunk(
                        tenant_id=tenant_id,
                        source_id=source_id,
                        chunk_index=batch_start + i,
                        content=chunk,
                        embedding=vec,
                    )
                )
            # Commit parcial por lote: progreso durable, no transacción gigante.
            await session.commit()
        source.status = "ready"
        await session.commit()
    except Exception:
        # La sesión puede estar en estado fallido tras un error SQL:
        # rollback antes de marcar la fuente.
        await session.rollback()
        src = await session.get(KnowledgeSource, source_id)
        if src is not None:
            src.status = "failed"
            await session.commit()
        raise
    return source_id


async def search_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    embedder: EmbedderPort,
    top_k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """Búsqueda por similitud coseno con filtro duro por tenant_id.

    Devuelve chunks con similarity >= threshold (umbral unificado del sistema).
    """
    _check_dimension(embedder)
    qvec = await embedder.embed(query)
    if len(qvec) != EMBED_DIM:
        raise ValueError(
            f"Vector de query con dimensión {len(qvec)} != {EMBED_DIM}"
        )
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
