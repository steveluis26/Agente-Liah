# DECISIONES FASE 5b — Plantillas escuela/danza + lente "ganar clientes"

2026-09-21. Cierre de producto sobre el esqueleto (rama `skeleton`).

## Tarea 1 — Dos plantillas de giro nuevas

- `templates/escuela_privada.yaml`: escuela privada ficticia (colegiaturas,
  inscripciones, horarios, ubicación y precios MXN inventados). La acción
  agendable son **visitas informativas** (recorridos para padres) con
  `book/cancel/reschedule_appointment`; el conocimiento cubre colegiaturas,
  admisión, horarios y visitas. Handoff forzado en: bullying/acoso,
  colegiaturas vencidas y adeudos, quejas sobre personal/alumnos, temas
  disciplinarios.
- `templates/academia_danza.yaml`: academia de danza ficticia (estilos,
  mensualidades, horarios, inscripción). La acción agendable son **clases de
  prueba gratuitas**; handoff forzado en: menores sin tutor, lesiones/salud,
  quejas sobre profesores.

Decisiones de diseño:

1. **Solo herramientas reales.** `build_tools()` expone hoy únicamente
   `search_knowledge_base`, `check_availability`, `book_appointment`,
   `cancel_appointment`, `reschedule_appointment` y `escalate_to_human`.
   NO existe tool de captura de leads: se documentó como pendiente dentro de
   cada plantilla (cabecera del YAML) en vez de inventar una.
2. **Reglas de pago como `custom` deshabilitadas.** El scheduler entiende
   `appointment_reminder`, `followup_30d` y `custom`; el recordatorio de
   pago de colegiatura/mensualidad se declaró `tipo: custom, enabled: false`
   con nota de que requiere implementación en el scheduler (fase 6). No se
   inventó ningún tipo de regla que el código no entienda.
3. **Cero código genérico tocado en la tarea 1**: solo YAML + docs + test.
   El schema ya validaba todo (`extra="forbid"`, herramientas contra
   `build_tools()`, horarios contra `_validate_business_hours`).

## Tarea 2 — Métricas del panel con lente "ganar clientes"

### Agregadas (baratas, con definición exacta)

- **`appointments_scheduled`** — "Citas agendadas". `COUNT(appointments)`
  del tenant con `created_at` en la ventana y `status != 'cancelled'`.
  Las crea el bot vía `book_appointment`; las canceladas no cuentan como
  valor generado. Sin desglose por canal: la tabla no tiene columna de
  canal (propuesta abajo).
- **`leads_captured`** — "Leads capturados". `COUNT(contacts)` del tenant
  con `created_at` en la ventana. Definición honesta: el webhook crea un
  `Contact` al primer mensaje de WhatsApp; como el bot es la puerta de
  entrada, un contacto nuevo = un lead capturado. **Límite conocido**: no
  hay `created_by`; si en el futuro se crean contactos por importación
  manual, la métrica se contamina (propuesta abajo).
- **`after_hours`** — "Mensajes fuera de horario atendidos". Cohorte:
  `messages` `inbound` en la ventana. Un mensaje está fuera de horario si
  su `created_at` (convención: UTC naive) convertido a `Tenant.timezone`
  cae fuera de `TenantConfig.business_hours` (misma forma validada por el
  schema; día ausente = cerrado). "Atendido" = existe un `outbound` del
  mismo contacto posterior al inbound (no distingue bot vs. humano).
  Devuelve `{outside_hours, attended, attended_pct}` o `None` si el tenant
  no tiene horarios (el panel muestra "—" en vez de un número mentiroso).
- **`avg_first_response_seconds`** — "1ª respuesta media": ya existía en el
  endpoint; se verificó que sigue visible en la UI (primera fila de KPIs de
  valor de negocio). Sin cambios de cálculo.

### UI

`app/templates/metrics.html` ahora abre con el encuadre "Lo que el
asistente generó por ti" y una fila de KPIs de negocio: Citas agendadas,
Leads capturados, Mensajes fuera de horario atendidos (X de Y), % atendidos
fuera de horario, 1ª respuesta media. Abajo siguen las métricas operativas
(conversaciones, resolución automática, transferencias) y las de costo.

### Pendientes (no implementados, con propuesta concreta)

1. **Inasistencias evitadas por recordatorios** — requiere estado de
   asistencia que no existe. Propuesta: agregar columna
   `appointments.attendance` (`NULL|attended|no_show`, migración Alembic),
   setearla desde el panel (bandeja: el operador marca al confirmar por
   teléfono) o por respuesta del contacto al HSM de recordatorio
   ("CONFIRMAR"/"CANCELAR" → `attended`/`cancelled`); métrica = citas con
   recordatorio enviado (`reminder_log`) que resultaron `attended` vs.
   tasa base de no-show del tenant. Sin esto, cualquier número sería
   inventado.
2. **Lead con origen trazable** — agregar `contacts.source`
   (`whatsapp_bot|manual_import|panel`, default `whatsapp_bot`) y/o
   `contacts.created_by`; la métrica pasaría a contar solo `whatsapp_bot`.
   Migración pequeña, sin cambios de flujo.
3. **Citas por canal** — agregar `appointments.channel`
   (`whatsapp|instagram|facebook`) poblado por la tool de booking desde el
   contexto del canal; métrica = `GROUP BY channel`. Relevante cuando el
   esqueleto sea multicanal (hoy todo es WhatsApp).

## Verificación

- `pytest`: 109 passed (baseline 101 + 5 de `test_templates_giros.py` + 3
  de `test_fase5b_metrics.py), 0 fallos.
- Las 4 plantillas validan con `load_template()` y `list_templates()` sin
  errores.
- Sin secretos en código ni docs; datos de test solo en la BD de test.
