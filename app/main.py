"""FastAPI app — punto de entrada Fase 0 + Fase 1 + Fase 2 (scheduler)."""
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.knowledge import router as knowledge_router
from app.api.tenants import router as tenants_router
from app.api.admin import router as admin_router
from app.api.onboarding import router as onboarding_router
from app.api.admin_ui import router as admin_ui_router
from app.channels.whatsapp.webhook import router as whatsapp_router
from app.reminders.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = None
    await _validate_embed_dim()
    if _scheduler_enabled():
        sched = start_scheduler()
    try:
        yield
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


async def _validate_embed_dim() -> None:
    """Falla rápido si la columna pgvector no coincide con EMBED_DIM (Fase 2).

    Un mismatch 1536 vs 768 rompe el RAG en silencio; mejor no arrancar.
    Si la BD no está disponible, se loguea advertencia (el resto de la app
    ya fallará por su cuenta al primer acceso).
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.agent.embedder import validate_embed_dim
    from app.core.db import async_session_maker

    try:
        async with async_session_maker() as session:
            dim = await validate_embed_dim(session)
        logging.getLogger("liah.startup").info(
            "EMBED_DIM validado contra pgvector: %s", dim
        )
    except RuntimeError:
        raise  # mismatch real: no arrancar
    except SQLAlchemyError as e:
        logging.getLogger("liah.startup").warning(
            "No se pudo validar EMBED_DIM (BD no disponible: %s)", e
        )


def _scheduler_enabled() -> bool:
    import os

    return os.getenv("ENABLE_REMINDER_SCHEDULER", "false").lower() == "true"


app = FastAPI(title="Agente Liah", version="0.4.0", lifespan=lifespan)

app.include_router(whatsapp_router)
app.include_router(knowledge_router)
app.include_router(tenants_router)
app.include_router(admin_router)     # API JSON del panel: /api/v1/admin
app.include_router(onboarding_router)  # alta desde plantilla: /api/v1/admin/tenants/onboard
app.include_router(admin_ui_router)  # UI server-rendered: /admin


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "phase": "4"}
