"""FastAPI app — punto de entrada Fase 0."""
import logging

from fastapi import FastAPI

from app.channels.whatsapp.webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="PYME Agent", version="0.0.1")

app.include_router(whatsapp_router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "phase": "0"}
