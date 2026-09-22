"""Transcripción de notas de voz (Fase 7f).

Contrato: `TranscriberPort` (`app/agent/ports.py`). El drenador transcribe
el audio entrante y el texto resultante alimenta el flujo normal del agente
como si fuera texto.

- `StubTranscriber` (default): no descarga nada, no llama a ningún
  proveedor; devuelve un marcador claro. Para dev/tests y para tenants sin
  transcriptor configurado.
- `WhisperTranscriber`: punto de extensión para el Whisper real. Hoy NO está
  implementado: `transcribe()` devuelve error explicativo (el drenador lo
  trata como fallo de transcripción y responde cortés, sin tumbar el job).

Dónde conectar el Whisper real (cuando se implemente):
1. Descargar el medio de Meta: el webhook entrega `audio.id` (media_id).
   GET https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{media_id}
   con `Authorization: Bearer <token del canal>` devuelve {"url": ...};
   luego GET a esa url (mismo Bearer) descarga los bytes del audio.
   El token se resuelve igual que en `app/agent/sender.py::_resolve_token`
   (SecretProvider por tenant; jamás hardcodear).
2. Transcribir: `openai.audio.transcriptions.create(model="whisper-1",
   file=bytes, language="es")` con la key del tenant (Fase 2: SecretProvider),
   o un faster-whisper local si se prefiere sin proveedor.
3. Devolver {"text": ..., "language": "es"}.

Config por tenant (TenantConfig.extra):
    {"transcriber": {"provider": "stub"}}      # default
    {"transcriber": {"provider": "whisper"}}   # futuro: ver arriba
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ports import TranscriberPort, TranscriptionResult
from app.models import TenantConfig

logger = logging.getLogger("liah.transcriber")

# Marcador inequívoco del stub: en tests y en dev se ve de inmediato que el
# texto NO es una transcripción real.
STUB_MARKER = (
    "[nota de voz recibida — transcripción de prueba (stub): "
    "el audio no se descargó ni se transcribió de verdad]"
)


class StubTranscriber:
    """Transcriptor de resguardo: devuelve un marcador claro, sin I/O."""

    async def transcribe(
        self, media_id: str, *, mime_type: str | None = None
    ) -> TranscriptionResult:
        logger.info("StubTranscriber: audio %s -> marcador", media_id)
        return {"text": STUB_MARKER, "language": None, "error": None}


class WhisperTranscriber:
    """Punto de extensión para Whisper real (no implementado aún).

    Ver el docstring del módulo para el cableado pendiente (descarga de Meta
    + API de OpenAI / faster-whisper local).
    """

    def __init__(self, session: AsyncSession, tenant_id):
        self._session = session
        self._tenant_id = tenant_id

    async def transcribe(
        self, media_id: str, *, mime_type: str | None = None
    ) -> TranscriptionResult:
        return {
            "text": None,
            "language": None,
            "error": (
                "transcriptor 'whisper' no implementado: falta conectar la "
                "descarga del medio desde Meta Graph API y la llamada a "
                "Whisper (ver app/agent/transcriber.py)"
            ),
        }


async def transcriber_for_tenant(
    session: AsyncSession, tenant_id
) -> TranscriberPort:
    """Resuelve el transcriptor según `TenantConfig.extra["transcriber"]`.

    Default: stub. Nunca levanta por config desconocida: un provider no
    reconocido cae al stub con warning (el drenador nunca debe morir por
    esto).
    """
    provider = "stub"
    try:
        cfg = (
            await session.execute(
                select(TenantConfig).where(
                    TenantConfig.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        extra = (cfg.extra if cfg else None) or {}
        provider = str((extra.get("transcriber") or {}).get("provider", "stub"))
    except Exception:  # noqa: BLE001 - config ilegible: stub y seguir
        logger.exception(
            "No se pudo leer config de transcriptor; uso stub"
        )
        provider = "stub"
    if provider == "whisper":
        return WhisperTranscriber(session, tenant_id)
    if provider != "stub":
        logger.warning(
            "Provider de transcriptor desconocido %r; uso stub", provider
        )
    return StubTranscriber()


__all__ = [
    "STUB_MARKER",
    "StubTranscriber",
    "WhisperTranscriber",
    "transcriber_for_tenant",
]
