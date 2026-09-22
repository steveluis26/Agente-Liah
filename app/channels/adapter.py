"""Contrato ChannelAdapter (Fase 1).

TODO canal de mensajería (WhatsApp, Instagram, Facebook, ...) vive detrás de
esta interfaz. El motor (`app.agent.engine`) y la cola (`queue.drain_jobs`)
solo hablan con `InboundEvent` y con el adapter registrado; nunca conocen
detalles del canal (wamid, phone_number_id, formatos de Meta, ...).

Para añadir un canal nuevo:
1. Implementar `ChannelAdapter` (normalmente en `app/channels/<canal>/`).
2. Registrarlo con `register_adapter(...)`.
3. Encolar trabajos con `payload["channel"] = <name>` para que el drenador
   resuelva el adapter correcto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Kinds de evento que el motor sabe manejar. Un canal puede mapear sus tipos
# nativos a estos; lo que no encaje va como "unsupported".
TEXT = "text"
AUDIO = "audio"  # Fase 7f: nota de voz; el drenador la transcribe a texto
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class InboundEvent:
    """Evento entrante agnóstico al canal."""

    sender_external_id: str  # id del remitente en el canal (p.ej. wa_id)
    external_message_id: str  # id del mensaje en el canal (p.ej. wamid; dedupe)
    kind: str  # "text" | "audio" | "unsupported"
    text: str = ""  # cuerpo si kind == "text" (o texto transcrito si "audio")
    sender_name: str | None = None  # nombre de perfil si el canal lo da
    raw: dict = field(default_factory=dict)  # payload original (auditoría)


class ChannelAdapter(Protocol):
    """Contrato que todo canal debe implementar."""

    name: str  # p.ej. "whatsapp"; coincide con payload["channel"]

    def parse_events(self, payload: dict) -> list[InboundEvent]:
        """Convierte el payload crudo del canal en eventos agnósticos.

        No debe hacer I/O: solo parseo puro. Los eventos sin
        `sender_external_id` o sin `external_message_id` se descartan aquí.
        """
        ...


_ADAPTERS: dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter


def get_adapter(name: str) -> ChannelAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(
            f"canal '{name}' sin ChannelAdapter registrado "
            f"(registrados: {sorted(_ADAPTERS)})"
        ) from None
