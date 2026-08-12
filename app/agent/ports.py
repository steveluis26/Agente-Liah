"""Puertos de abstracción (desacoplamiento del proveedor).

Cada puerto es una interfaz (Protocol). Las implementaciones concretas viven en
submódulos (openai_llm, memory/calcom calendar, openai embedder). Esto evita
lock-in y mantiene el loop del agente ignorante del proveedor.
"""
from typing import Any, Protocol, runtime_checkable

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


class LLMResponse:
    """Resultado normalizado de una llamada al LLM.

    - finish_reason == "tool_calls": `tool_calls` lleva la(s) herramienta(s).
    - finish_reason == "stop": `content` lleva el texto final.
    """

    def __init__(
        self,
        content: str | None,
        finish_reason: str,
        tool_calls: list[dict] | None = None,
        raw: Any = None,
    ):
        self.content = content
        self.finish_reason = finish_reason
        self.tool_calls = tool_calls or []
        self.raw = raw

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
@runtime_checkable
class CalendarPort(Protocol):
    """Fuente de verdad de disponibilidad y reservas del tenant."""

    async def check_availability(self, date: str, time_slot: str) -> dict:
        """Devuelve {'available': bool, 'alternatives': [...]}."""
        ...

    async def book(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
    ) -> dict:
        """Reserva y devuelve {'ok': bool, 'event_id': str|None, 'start_at': str|None}."""
        ...
