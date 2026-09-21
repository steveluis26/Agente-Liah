"""API de ingesta de conocimiento (Fase 2).

POST /tenants/{tenant_id}/knowledge  -> ingesta texto (split/embed/insert).

El embedder lo decide la CONFIG DEL TENANT (`tenant_configs.model_routing`
["embedder"]: openai|ollama|fake), nunca el cliente: el flag `use_openai`
del body se eliminó en Fase 2 (era un vector de abuso: el cliente elegía
consumir API de pago).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import build_embedder_for_tenant
from app.agent.rag import ingest_knowledge
from app.agent.secrets import EnvSecretProvider
from app.core.db import get_session
from app.models import Tenant, TenantConfig

router = APIRouter(prefix="/tenants", tags=["knowledge"])


class KnowledgeIngest(BaseModel):
    title: str
    content: str


@router.post("/{tenant_id}/knowledge")
async def ingest(
    tenant_id: uuid.UUID,
    body: KnowledgeIngest,
    session: AsyncSession = Depends(get_session),
):
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    routing = dict(cfg.model_routing or {}) if cfg else {}
    # El factory necesita el slug para resolver la key del tenant: se lo
    # pasamos por una clave interna (nunca sale al cliente).
    tenant = await session.get(Tenant, tenant_id)
    routing["_tenant_slug"] = tenant.slug if tenant else ""
    try:
        embedder = build_embedder_for_tenant(routing, EnvSecretProvider())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    source_id = await ingest_knowledge(
        session, tenant_id, body.title, body.content, embedder
    )
    return {"source_id": str(source_id), "status": "ready"}
