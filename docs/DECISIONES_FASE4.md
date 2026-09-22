# Decisiones Fase 4 — Alta por perfil declarativo + plantillas por giro (2026-09-22)

El alta de un cliente deja de ser inserción manual de filas: es **aplicar un
perfil declarativo versionado** (`templates/<giro>.yaml`) con overrides
puntuales, en **una sola transacción**, vía CLI (`scripts/onboard_tenant.py`)
o panel (`/admin/onboard` → `POST /api/v1/admin/tenants/onboard`).

## Perfil declarativo: `app/core/profile_schema.py`

- Modelos Pydantic estrictos (`extra="forbid"` en todos): `schema_version`
  debe ser `"1.0"`, `slug` con regex, `timezone` validado como zona IANA,
  `horarios` con el validador del panel, y `tools` **contra los nombres
  reales de `build_tools()`** — un typo en la plantilla falla en validación,
  no en producción a las 3am.
- `model_routing` se valida con el mismo `ModelRoutingUpdate` de la API de
  config: lo que el perfil declara es exactamente lo que el factory de
  agentes consume.
- `load_template()` / `list_templates()` / `apply_overrides()`: los overrides
  son un merge superficial (shallow) documentado; los arrays del perfil
  (tools, templates, reglas, conocimiento) se **reemplazan completos**, no
  se concatenan. `system_prompt` ausente → `ValidationError` clara.

## Plantillas

- `templates/consultorio_medico.yaml` (giro objetivo comercial; demo de
  Fase 5): clínica ficticia, servicios/precios MXN, políticas de
  cancelación/reprogramación, recordatorios 24h/2h **con consentimiento**,
  handoff para urgencias/diagnóstico/recetas/petición de humano, 2 reglas,
  4 HSM, 4 documentos semilla.
- `templates/estetica.yaml`: segundo ejemplo mínimo para demostrar que el
  mecanismo es genérico (1 regla, 2 HSM, 3 documentos).
- Las HSM nacen como filas `templates` **pendientes de aprobación de Meta**:
  el perfil no puede aprobar plantillas; el alta registra el estado real
  (`templates.name`) para que el operador las cree/apruebe en el panel de
  Meta y el scheduler solo las use cuando existan.

## Transacción única: `app/api/onboarding.py::onboard_tenant()`

- Crea en una sola transacción: tenant + API key, `tenant_configs`,
  automation_rules, templates, conocimiento semilla (fuente + chunks con
  embeddings vía `ingest_knowledge(commit=False)`), `tenant_admin`
  (password hasheada PBKDF2), y el evento `tenant.onboarded`.
- `commit=False` en `rag.ingest_knowledge()`: el onboarding es dueño de la
  transacción; un fallo a mitad de ingesta hace rollback de **todo**
  (probado con un embedder que revienta en el 2º documento).
- El CLI y la API llaman al **mismo servicio** (sin duplicación de lógica);
  la única diferencia es la inyección del embedder (tests: fake; prod: el
  embedder del routing).
- Slug o email de admin duplicados → `OnboardingError` → rollback total,
  exit code 2 en el CLI.

## Defaults comerciales

- `DEFAULT_MODEL_ROUTING = {llm_provider: openai, llm_model: gpt-4o-mini,
  embedder: openai, ...}`: el producto se vende como servicio gestionado;
  Ollama queda como opción explícita por override, no como default.
- Password del tenant admin **solo transitoria**: env
  `LIAH_TENANT_ADMIN_PASSWORD` o prompt `getpass`; mínimo 12 caracteres;
  nunca se loguea ni se devuelve; la API key del tenant se muestra **una
  sola vez** (como los proveedores serios).

## Herramientas por perfil

- `cancel_appointment` / `reschedule_appointment` nuevas en
  `app/agent/tools.py`; `AppointmentType` se generalizó
  (`consultation|followup|other`) — el motor ya no conoce giros.
- `CalendarPort.cancel()` / `.reschedule()`: reprogramar = cancelar + crear
  **atómicamente**; si el nuevo slot está ocupado, rollback conserva la cita
  original (probado).

## Recordatorios: regla genérica `appointment_reminder`

- `params = {hours_before: [24, 2], template_name, require_consent}`:
  `scheduled_for = start_at − h` por cada h; el scheduler resuelve el
  template por **nombre**, no por tipo de regla.
- `reminder_log.appointment_id` (nullable, FK a appointments,
  ON DELETE SET NULL; migración `f4_onboarding`): la idempotencia es
  (rule, contact, appointment, scheduled_for). Sin esto, dos citas del mismo
  contacto que calculen el mismo `scheduled_for` se pisaban (el segundo
  recordatorio se suprimía como duplicado) — lo encontró un test de Fase 4
  y se corrigió aquí, no en Fase 5.
- El gate de consentimiento sigue en `dispatch_reminder` (LFPDPPP, siempre),
  no en la regla: `require_consent: false` no existe como opción.

## Limpieza del vertical legacy

- `scripts/demo_academia.py`, `demo_tests_extra.py`,
  `_verify_demo_pipeline.py`, `seed_fase0.py` → `scripts/legacy/` con
  `README.md` de advertencia: pertenecen al demo de academia, sus PASS son
  engañosos y hacen `drop_all()` sin protección. Fase 5 reescribe el demo
  como consultorio médico desde cero.
- `app/` queda sin literales del vertical (`trial_class`, `colegiatura`,
  salsa/bachata/ballet, academia): el motor es agnóstico al giro.

## Sin migración… casi

- `migrations/versions/f4_onboarding.py` solo agrega
  `reminder_log.appointment_id` (ver arriba). Todo lo demás del onboarding
  reutiliza tablas existentes — documentado aquí para que nadie busque una
  migración que no hace falta. Cadena verificada `f1→f2→f3→f4` con
  `alembic upgrade head` sobre BD vacía.

## UI

- `/admin/onboard` (solo `platform_admin`; el link solo se renderiza para
  ese rol): selector de plantilla, campos nombre/slug/email/password,
  textarea de overrides JSON, y muestra de la API key una sola vez.
  Reutiliza la cookie JWT del panel; sin build step (Jinja2).
