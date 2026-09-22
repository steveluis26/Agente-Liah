"""FastAPI app — punto de entrada Fase 0–5.

Scheduler de recordatorios: en DEV puede correr in-process vía el lifespan
(`ENABLE_REMINDER_SCHEDULER=true`). En PRODUCCIÓN se usa el worker dedicado
(`python -m app.worker`, ver `app/worker.py`) con el in-process apagado: si
ambos corren, los recordatorios se evaluarían dos veces (la idempotencia por
`reminder_log` evita duplicados, pero es desperdicio).
"""
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.knowledge import router as knowledge_router
from app.api.tenants import router as tenants_router
from app.api.admin import router as admin_router
from app.api.onboarding import router as onboarding_router
from app.api.admin_ui import router as admin_ui_router
from app.channels.whatsapp.webhook import router as whatsapp_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.ratelimit import RateLimitMiddleware
from app.reminders.scheduler import start_scheduler

settings = get_settings()
setup_logging(json_format=settings.liah_log_json)


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


app = FastAPI(title="Agente Liah", version="0.8.0", lifespan=lifespan)

# Fase 8: CORS explícito (default: sin orígenes extra, solo mismo origen).
_cors_origins = [o.strip() for o in settings.liah_cors_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

# Fase 8: rate limiting anti-abuso (/health exento).
if settings.liah_rate_limit_enabled:
    app.add_middleware(RateLimitMiddleware)

app.include_router(whatsapp_router)
app.include_router(knowledge_router)
app.include_router(tenants_router)
app.include_router(admin_router)     # API JSON del panel: /api/v1/admin
app.include_router(onboarding_router)  # alta desde plantilla: /api/v1/admin/tenants/onboard
app.include_router(admin_ui_router)  # UI server-rendered: /admin


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "phase": "5"}
