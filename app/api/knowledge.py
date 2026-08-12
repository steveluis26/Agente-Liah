"""API de ingesta de conocimiento (Fase 1).

POST /tenants/{tenant_id}/knowledge  -> ingesta texto (split/embed/insert).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.embedder import FakeEmbedder, OpenAIEmbedder
from app.agent.rag import ingest_knowledge
from app.core.db import get_session

router = APIRouter(prefix="/tenants", tags=["knowledge"])


class KnowledgeIngest(BaseModel):
    title: str
    content: str
    use_openai: bool = False  # si True usa OpenAIEmbedder (requiere OPENAI_API_KEY)


@router.post("/{tenant_id}/knowledge")
async def ingest(
    tenant_id: uuid.UUID,
    body: KnowledgeIngest,
    session: AsyncSession = Depends(get_session),
):
    try:
        embedder = OpenAIEmbedder() if body.use_openai else FakeEmbedder()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    source_id = await ingest_knowledge(
        session, tenant_id, body.title, body.content, embedder
    )
    return {"source_id": str(source_id), "status": "ready"}
