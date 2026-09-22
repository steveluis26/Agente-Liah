# DECISIONES — Fase 7f: modo staff por WhatsApp + briefings + notas de voz

Fecha: 2026-09-21. Rama: `skeleton` (sin commit; cambios en working tree).

## Qué se construyó

1. **Modelo `StaffMember`** (`app/models/staff.py` + migración `f7f_staff`
   encadenada a `f7_scheduling`): `tenant_id`, `wa_id` (único por tenant),
   `nombre`, `role` ∈ {owner, specialist, receptionist},
   `resource_id` nullable → `resources.id` (para specialist: su recurso en
   la agenda).
2. **Detección staff vs cliente** (`app/channels/whatsapp/queue.py::_process_job`):
   el lookup `get_staff_member(tenant_id, wa_id)` va ANTES de crear el
   `Contact` y ANTES del consentimiento de privacidad. Un número staff jamás
   recibe el aviso de privacidad ni genera un contacto de cliente. Dedupe
   ligero por wamid sin persistir el inbound del staff.
3. **Comandos en lenguaje natural** (`app/agent/staff.py`): intents
   deterministas por patrones en español (normalización sin acentos):
   `agenda_today` ("mi agenda de hoy"), `who_at` ("¿quién es el de las
   10:30?"), `free_tomorrow` ("¿qué huecos hay mañana?"); lo demás →
   ayuda con ejemplos. Alcance por rol: specialist filtra por su
   `resource_id` vía `appointment_resources` (agenda y `who_at`) y solo ve
   tipos de servicio que usan su recurso (huecos); owner/receptionist ven
   todo. Huecos = escaneo del día siguiente 08:00–20:00 en pasos de 30 min
   con el motor real de disponibilidad (máx. 8 por tipo de servicio).
   **Punto de extensión LLM**: reemplazar `_parse_intent` por un
   clasificador que devuelva el mismo vocabulario `(intent, params)`; los
   ejecutores `_cmd_*` no cambian.
4. **Resumen matutino** (`app/reminders/staff_briefing.py::send_due_staff_briefings`):
   config en `TenantConfig.extra["staff_briefing"] =
   {enabled, hour: "HH:MM", roles: [...]}` (se edita con el endpoint de
   config del panel ya existente). Es "due" si la hora configurada de hoy
   (zona del tenant) ya pasó y estamos dentro de 45 min; idempotencia por
   `idempotency_key=staff-briefing:{tenant}:{YYYY-MM-DD}:{staff}` (registrada
   en `action_log` incluso en dry-run). Enganchado en el worker
   (`run_cycle(..., run_briefings=True)`) y en el scheduler in-process
   (cada 15 min). Cada rol recibe su alcance (specialist: solo lo suyo).
5. **Alertas** (`app/agent/staff_notify.py::notify_staff`, helper con
   `SenderPort`): al cancelar (`calendar.cancel`), al ofrecer hueco de
   waitlist (`waitlist.offer_on_cancel`) y al crear handoff
   (`tools.escalate_to_human`). Enganches mínimos con try/except: **un fallo
   notificando jamás revierte la operación principal** (la notificación va
   después del commit). Roles default: owner + receptionist.
6. **Notas de voz**: `TranscriberPort` (`app/agent/ports.py`) +
   `app/agent/transcriber.py` con `StubTranscriber` (devuelve marcador
   claro, sin I/O) y `WhisperTranscriber` (punto de extensión: hoy devuelve
   error explicativo; el docstring documenta el cableado pendiente:
   descarga del medio vía `GET graph.facebook.com/{v}/{media_id}` con el
   token del canal —igual que `sender._resolve_token`— y luego
   `openai.audio.transcriptions` o faster-whisper local). Config por tenant:
   `extra["transcriber"] = {"provider": "stub"|"whisper"}`, default stub.
   El adapter parsea `audio` → kind `audio` (nuevo en
   `app/channels/adapter.py`); el drenador transcribe y el texto alimenta el
   flujo normal como si fuera texto (consentimiento, keywords, agente). Si
   falla → respuesta cortés + `message.transcription_failed`, sin tumbar el
   job.
7. **Identidad de canal del staff**: `notify_staff` asegura un `Contact`
   con `contact_type="staff"` por wa_id (reclasifica si el número era
   cliente). Queda fuera de la segmentación cliente/curioso (campañas solo
   aceptan client|prospect) y del listado del panel (`list_contacts`
   excluye "staff" por defecto).

## Decisiones y por qué

- **Comandos deterministas, no LLM**: predecible, testeable, sin costo por
  mensaje; el vocabulario de intents es el contrato para un futuro
  clasificador LLM.
- **Config en `TenantConfig.extra`, no columnas nuevas**: el panel ya edita
  `extra`; evita otra migración y otro formulario.
- **Ventana de 45 min + idempotencia en vez de cron exacto**: el worker corre
  por intervalos; así no se pierde el briefing si un ciclo se retrasa, y no
  se duplica.
- **Staff como `Contact` contact_type="staff"**: reutiliza `Message`/
  `ActionLog` (auditoría e idempotencia gratis) sin contaminar la
  segmentación de clientes.
- **`send_text` del adapter ahora propaga el `SendResult` real** (antes
  devolvía siempre "dry_run" en dry-run, ocultando `skipped_duplicate`):
  necesario para que el briefing cuente envíos reales. `send_message()` se
  conserva como helper público.

## Pruebas

`tests/test_fase7f_staff.py`: 19 tests (detección staff vs cliente con
contraste de aviso de privacidad; scoping specialist/owner; 3 comandos;
ayuda; briefing a la hora / fuera de hora / disabled / idempotente;
alertas en cancel, waitlist-offer y handoff; stub + config del transcriptor;
audio de cliente alimenta al agente; audio de staff va al manejador).

## Huecos / pendientes honestos

- `WhisperTranscriber` no implementado (ver sección 6 para el cableado).
- El staff no puede *modificar* la agenda (cancelar/reagendar por comando):
  solo lectura + alertas. Siguiente paso natural.
- `contact_type="staff"` no aparece como filtro en el panel (el listado lo
  excluye por defecto); si se quiere gestionar el staff desde el panel,
  falta UI de alta/baja (hoy es por BD o API directa).
- Los comandos entienden "hoy"/"mañana" implícitos; fechas explícitas
  ("agenda del viernes") no están soportadas.
- El briefing usa texto libre (ventana 24h); fuera de ventana se necesitaría
  plantilla HSM aprobada como en recordatorios.
