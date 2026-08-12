"""FastAPI app — punto de entrada Fase 0 + Fase 1."""
import logging

from fastapi import FastAPI

from app.api.knowledge import router as knowledge_router
from app.channels.whatsapp.webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Agente Liah", version="0.1.0")

app.include_router(whatsapp_router)
app.include_router(knowledge_router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "phase": "1"}
