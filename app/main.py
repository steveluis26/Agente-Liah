"""FastAPI app — punto de entrada Fase 0 + Fase 1 + Fase 2 (scheduler)."""
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.knowledge import router as knowledge_router
from app.api.tenants import router as tenants_router
from app.channels.whatsapp.webhook import router as whatsapp_router
from app.reminders.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = None
    if _scheduler_enabled():
        sched = start_scheduler()
    try:
        yield
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


def _scheduler_enabled() -> bool:
    import os

    return os.getenv("ENABLE_REMINDER_SCHEDULER", "false").lower() == "true"


app = FastAPI(title="Agente Liah", version="0.2.0", lifespan=lifespan)

app.include_router(whatsapp_router)
app.include_router(knowledge_router)
app.include_router(tenants_router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "phase": "2"}
