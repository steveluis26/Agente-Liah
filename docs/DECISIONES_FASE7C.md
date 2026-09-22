# DECISIONES_FASE7C — Motor de disponibilidad por recursos + consentimiento + lista de espera

Fase 7c construye sobre el esquema declarativo de Fase 7b (`Resource`,
`ServiceType`, `TenantPrivacyTerms`, `AppointmentResource`, `WaitlistEntry`,
`Appointment.service_type_slug/venue`, `Contact.privacy_terms_version`):
el engine de agenda ahora **usa** esos modelos. Cero literales de vertical
en Python: todo lo variable por negocio (recursos, duraciones, buffers,
traslados, textos de aviso) vive en la BD del tenant.

## 1. Motor de disponibilidad (`app/agent/availability.py`)

`check_resource_availability(session, tenant_id, service_type_slug, date,
start_hhmm, venue=None, exclude_appointment_id=None, contact_id=None)`
→ `{"available", "reason", "conflicts", "alternatives"}`.

- **Resolución de recursos**: `{"recurso": slug}` → el recurso exacto;
  `{"tipo": t, "cantidad": n, "especialidad": e}` → los primeros n por slug
  entre los del tipo t (filtrando especialidad si se da). Si no hay
  suficientes → no disponible con motivo claro. Sin duplicados.
- **Intervalo ocupado** = `[inicio − setup, inicio + duración + teardown]`.
  Cada cita existente se mide con los buffers de **su propio** tipo de
  servicio (las citas legacy sin tipo usan los del candidato; documentado
  como fallback explícito).
- **Capacidad**: traslapes confirmados sobre el intervalo ≥ capacidad →
  conflicto (`resource_capacity`). "No traslapar personas ni lugares".
  El borde exacto (termina 10:15, empieza 10:15) NO es traslape.
- **Traslados** (solo `movilidad == "mobile"`): vecinos anterior/siguiente
  del día por intervalo ocupado; `hueco ≥ travel(venue_previo, venue)` en
  ambas direcciones.
- **Matcheo de zona** (diseño simple y testeado): el venue destino se
  normaliza (minúsculas, sin acentos) y se compara por **igualdad exacta**
  contra las claves de `zonas` (también normalizadas); sin coincidencia →
  `default_min`. Origen == destino (o ambos vacíos) → 0 siempre. El operador
  nombra las zonas con los labels que usa en `venue` (p.ej. `{"xalapa": 0,
  "veracruz": 45}`); las etiquetas genéricas de las plantillas
  (`misma_sede/misma_ciudad/otra_ciudad`) son ejemplos, no magia.
- **Traslape de persona**: si se da `contact_id`, sus propias citas
  confirmadas (cualquier recurso) que pisen el intervalo → conflicto.
- **Alternativas**: ante conflicto real, se prueban slots cercanos con el
  propio motor (mismo día 8:00–20:00 en pasos de 30 min, luego misma hora
  días siguientes; tope 3, sin recursión). Errores de config (tipo
  desconocido, recurso irresoluble) devuelven `[]` con motivo.
- **Servicio sin recursos requeridos**: solo aplica el traslape de persona
  (un servicio virtual no bloquea a nadie más). Decisión explícita.

## 2. Calendario (`app/agent/calendar.py`)

- `check_availability(..., service_type_slug=None, venue=None,
  contact_id=None)`: con `service_type` delega al motor; sin él, el
  comportamiento legacy intacto (los tests viejos llaman sin service_type).
- `book(..., service_type_slug=None, venue=None)`: con service_type valida
  con el motor y crea cita + filas `AppointmentResource` en la misma
  transacción; conserva idempotencia (ActionLog) y el guard del índice
  único. `end_at` = inicio + duración (las citas legacy lo dejan NULL).
- `reschedule(..., service_type_slug=None, venue=None)`: el nuevo slot se
  valida con el motor (excluyendo la cita vieja); la atomicidad se conserva
  (si el nuevo slot falla, rollback restaura la vieja).
- `cancel(..., notify_waitlist=True, waitlist_sender=None)` devuelve
  además `freed_slot = {service_type_slug, start_at, venue}` y ofrece el
  hueco a la lista de espera. **Decisión**: el hueco de un `reschedule` NO
  se ofrece (la oferta hace commit y rompería la atomicidad del
  cancela+reserva). Un fallo en la oferta no revierte la cancelación
  (se reporta en `waitlist_error`).
- `CalComAdapter` y `CalendarPort` (`app/agent/ports.py`) actualizados con
  las mismas firmas; `CancelResult` suma `freed_slot` opcional.

**Limitación honesta (sin migración en esta fase)**: el índice único
`(tenant_id, start_at)` impide dos citas en el instante exacto aunque usen
recursos distintos. Capacidad > 1 funciona con inicios distintos
(traslapes reales); el mismo minuto exacto lo rechaza el guard de BD.
Relajarlo requiere migración (fuera del alcance de esta fase).

## 3. Consentimiento de privacidad (`app/agent/consent.py`)

Máquina de estados determinista (sin LLM), canal-agnóstica:

- `none`/`pending` + mensaje normal → responde PRIMERO con
  `TenantPrivacyTerms.texto` + petición de aceptación; `pending`.
- Afirmativo (`sí/si/acepto/aceptar/de acuerdo/ok/confirmo`,
  case-insensitive, sin acentos, por límites de palabra) → `granted` +
  `consent_at` + `privacy_terms_version` = versión vigente; **el flujo
  normal continúa con ese mismo mensaje**.
- Negativa clara (`no` pelado / `no acepto`) → `revoked` + respuesta mínima
  (`Sin tu aceptación no podemos atenderte por este medio ni guardar tus
  datos`). La negativa **siempre gana** ante empate (criterio heredado de
  marketing).
- `granted` con versión vieja → `pending` + reenvío del aviso (re-aceptar;
  decisión Fase 7b).
- Sin fila de términos → puerta **abierta** (tenants legacy / plantillas
  1.0 siguen funcionando sin cambios).

**Dónde engancha** (decisión de diseño): en `drain_jobs` /
`_process_job` (`app/channels/whatsapp/queue.py`), justo después de
persistir el inbound y **antes** del opt-in de marketing y del agente —
el mismo patrón del gate de keywords de Fase 6. Motivos: (a) la privacidad
es prerrequisito de todo procesamiento posterior (incluido el marketing);
(b) determinista y testeable sin LLM; (c) la máquina de estados vive en
`app/agent/consent.py`, agnóstica del canal, reutilizable cuando lleguen
Instagram/Facebook; el engine sigue enfocado en el loop LLM.
Además `_get_or_create_contact` ya no persiste `name` sin `granted`
(minimización de datos); se persiste en el siguiente mensaje tras otorgar.

**Gating en tools** (`app/agent/tools.py`): `book_appointment` y
`reschedule_appointment` fallan con error claro si el contacto no tiene
`granted` + versión vigente. `cancel_appointment` NO se gatea (cancelar es
un derecho, no requiere consentimiento). El engine además propaga
`service_type`/`venue` del book al pre-check de disponibilidad, para que el
guard anti-fuente-de-verdad valide lo mismo que se va a reservar.

## 4. Lista de espera (`app/agent/waitlist.py`)

- `join_waitlist()`: idempotente (no duplica `waiting` por
  contacto+tipo).
- `offer_on_cancel(session, tenant_id, freed, sender, dry_run)`: entradas
  `waiting` del mismo tipo ordenadas por antigüedad; cada una se valida con
  el motor (excluyendo la cita cancelada); a la **primera** que califique →
  `offered` + aviso por el canal. **NO auto-agenda**: la conversión ocurre
  cuando el contacto confirma (p.ej. `reschedule_appointment`).
- El envío usa `SenderPort`; sin sender inyectado se construye el de
  WhatsApp con `dry_run` según `LIAH_SEND_DRY_RUN` (default dry-run).
  Mensaje genérico con nombre del servicio, fecha, hora y venue.
- Enganche: `MemoryCalendarAdapter.cancel` (con `notify_waitlist` /
  `waitlist_sender` para tests).

## 5. Huecos vistos (no resueltos en esta fase)

1. **Índice único `(tenant_id, start_at)`** vs. capacidad > 1 en el minuto
   exacto (ver §2). Requiere migración.
2. **Ventana 24h de WhatsApp**: la oferta de waitlist fuera de la ventana
   de conversación debería usar plantilla HSM aprobada (hoy es texto
   libre; en producción con `LIAH_SEND_DRY_RUN=0` podría fallar por
   política de Meta).
3. **Conversión de la oferta** (`offered → converted`): el flujo "el
   contacto responde que sí" aún no convierte la oferta en cita
   automáticamente; hoy el contacto debe pedir el alta (reschedule).
4. **Reschedule no ofrece el hueco viejo** a la waitlist (decisión
   consciente por atomicidad, §2).
5. **Nombre tras otorgar**: se persiste en el siguiente mensaje, no en el
   del otorgamiento (el adapter ya corrió). Aceptable; documentado.
6. **Consentimiento y modo humano**: el gate corre antes del gate de modo
   humano (la privacidad es prerrequisito legal aunque un humano atienda).
7. **Zona vs. ciudad real**: el matcheo de zonas es por label exacto, no
   geográfico; el operador debe mantener `zonas` alineado con los `venue`
   que usa.
