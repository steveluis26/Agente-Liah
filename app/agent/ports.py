"""Puertos de abstracción (desacoplamiento del proveedor).

Cada puerto es una interfaz (Protocol). Las implementaciones concretas viven en
submódulos (openai_llm, memory/calcom calendar, whatsapp sender). Esto evita
lock-in y mantiene el loop del agente ignorante del proveedor.

Los dicts de retorno están tipados con TypedDict para que el engine y los
tests no dependan de claves mágicas.
"""
from typing import Any, Protocol, TypedDict, runtime_checkable

# ── LLM ──────────────────────────────────────────────
@runtime_checkable
class LLMPort(Protocol):
    """Modelo de lenguaje con tool-calling nativo."""

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> "LLMResponse":
        """Devuelve una respuesta que puede ser texto o una llamada a tool."""
        ...


class LLMUsage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LLMResponse:
    """Resultado normalizado de una llamada al LLM.

    - finish_reason == "tool_calls": `tool_calls` lleva la(s) herramienta(s).
    - finish_reason == "stop": `content` lleva el texto final.
    - `usage`: tokens consumidos (Fase 2 los persiste para costeo).
    """

    def __init__(
        self,
        content: str | None,
        finish_reason: str,
        tool_calls: list[dict] | None = None,
        raw: Any = None,
        usage: LLMUsage | dict | None = None,
    ):
        self.content = content
        self.finish_reason = finish_reason
        self.tool_calls = tool_calls or []
        self.raw = raw
        self.usage: LLMUsage = dict(usage or {})

    @property
    def is_tool_call(self) -> bool:
        return self.finish_reason == "tool_calls" and bool(self.tool_calls)


# ── Embedder ─────────────────────────────────────────
@runtime_checkable
class EmbedderPort(Protocol):
    """Convierte texto en vector."""

    dimension: int

    async def embed(self, text: str) -> list[float]:
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        ...


# ── Calendario ───────────────────────────────────────
class AvailabilityResult(TypedDict, total=False):
    available: bool
    alternatives: list[str]  # ["YYYY-MM-DD HH:MM", ...] slots libres reales
    error: str | None


class BookingResult(TypedDict, total=False):
    ok: bool
    event_id: str | None
    start_at: str | None  # ISO
    cancelled_event_id: str | None  # solo reschedule: la cita que se canceló
    alternatives: list[str]  # slots libres reales si no había cupo
    error: str | None


class CancelResult(TypedDict, total=False):
    ok: bool
    event_id: str | None  # cita cancelada
    error: str | None
    freed_slot: dict | None  # hueco liberado p/la lista de espera:
    # {"service_type_slug": str|None, "start_at": ISO|None, "venue": str|None}


@runtime_checkable
class CalendarPort(Protocol):
    """Fuente de verdad de disponibilidad y reservas del tenant."""

    async def check_availability(
        self, date: str, time_slot: str,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> AvailabilityResult:
        """Devuelve disponibilidad + alternativas reales si no hay cupo.

        Con `service_type_slug` usa el motor de recursos (Fase 7c); sin él,
        el comportamiento legacy por slot.
        """
        ...

    async def book(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> BookingResult:
        """Reserva y devuelve el resultado.

        Si `idempotency_key` ya existe en action_log, devuelve el resultado
        guardado SIN crear otra cita (reintentos seguros).
        """

    async def cancel(
        self, contact_id: str, date: str, time_slot: str
    ) -> "CancelResult":
        """Cancela la cita confirmada del contacto. Idempotente por naturaleza:
        si no hay cita, devuelve ok=False sin efectos."""
        ...

    async def reschedule(
        self,
        contact_id: str,
        old_date: str,
        old_time_slot: str,
        new_date: str,
        new_time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
    ) -> "BookingResult":
        """Cancela la cita vieja y reserva la nueva en una transacción: si el
        nuevo slot falla, la cita original se conserva."""
        ...
        ...


# ── Transcriptor (notas de voz) ──────────────────────────────
# Fase 7f: las notas de voz entrantes se transcriben y el texto alimenta el
# flujo normal del agente como si fuera texto. Puerto intercambiable: el
# stub sirve para dev/tests; el Whisper real se conecta donde documenta
# `app/agent/transcriber.py`.
class TranscriptionResult(TypedDict, total=False):
    text: str | None       # texto transcrito (None si falló)
    language: str | None   # idioma detectado (si el proveedor lo da)
    error: str | None


@runtime_checkable
class TranscriberPort(Protocol):
    """Convierte un audio entrante (media_id del canal) en texto."""

    async def transcribe(
        self, media_id: str, *, mime_type: str | None = None
    ) -> TranscriptionResult:
        """Transcribe el audio identificado por `media_id`.

        Nunca levanta: ante un fallo devuelve {"text": None, "error": ...}.
        """
        ...


# ── Sender (canal de salida) ─────────────────────────
# Primer paso del desacoplamiento multicanal (refinamiento 2026-09-21): el
# motor/orquestador habla contra SenderPort, no contra WhatsApp. El adaptador
# concreto (WhatsApp Cloud API hoy; Instagram/Facebook mañana) implementa el
# contrato. Prohibido hardcodear supuestos del canal en el engine.
class SendResult(TypedDict, total=False):
    message_id: str | None      # id local del mensaje persistido
    meta_message_id: str | None  # id devuelto por el proveedor (wamid)
    status: str                # sent|dry_run|skipped_duplicate|failed
    error: str | None


@runtime_checkable
class SenderPort(Protocol):
    """Contrato de envío del orquestador hacia cualquier canal."""

    async def send_text(
        self,
        tenant_id: str,
        contact_id: str,
        channel_contact_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SendResult:
        """Envía texto al contacto del canal (wa_id en WhatsApp).

        Idempotente por `idempotency_key`: si ya se envió, no duplica.
        """
        ...
