import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.agent.embedder import EMBED_DIM
from app.core.base import Base, TenantMixin

# La dimensión vive en app/agent/embedder.py (EMBED_DIM, única fuente de
# verdad) y se valida contra esta columna al arranque (validate_embed_dim).
# Producción: OpenAI (1536). Demo local: Ollama/nomic-embed-text (768) vía
# env EMBED_DIM=768 ANTES de crear el esquema.


class KnowledgeSource(Base, TenantMixin):
    __tablename__ = "knowledge_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # pdf|text|url|faq
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)


class KnowledgeChunk(Base, TenantMixin):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
