# Decisiones Fase 5 — Demo end-to-end + worker + guía (2026-09-21)

Última fase de construcción del esqueleto. Todo lo que sigue se verificó
con `pytest` (101 passed) y con la demo real (`DEMO OK (4/4 rutas)`).

## 1. `scripts/demo_consultorio.py` (nueva, desde cero)

- **Clínica ficticia y genérica** dada de alta con la plantilla
  `consultorio_medico` vía el **onboarding real**
  (`app/api/onboarding.py::onboard_tenant`, la misma función que usan el CLI
  y el endpoint): transacción única, FakeEmbedder, sin `drop_all` sorpresa.
- **BD propia: `pyme_agent_demo`** (se crea si no existe). No usa la BD de
  test de pytest: la demo es una herramienta de operador, no un test, y debe
  poder correrse sin interferir con la suite.
- **Sin borrado por default**: solo `--reset` / `LIAH_DEMO_RESET=1` hace
  `drop_all`. Sin reset, el tenant se reusa por slug y la demo limpia **sus
  propios artefactos** del run anterior (citas del contacto demo + sus
  `action_log` de booking). La ruta 3 usa un contacto con `wa_id` único por
  corrida para no mezclar evidencia de handoffs.
- **Veredictos sobre resultados reales**, no retrieval:
  - (1) Conocimiento: StubLLM determinista que **hace eco del tool result**
    del RAG (no alucina el precio). Si el RAG no recupera nada, la respuesta
    final no trae `$600` y el veredicto falla — que es exactamente lo que se
    quiere probar (el PASS engañoso de la demo vieja de academia).
  - (2) Acción: booking/cancelación/reprogramación por `run_agent` (ruta
    completa del engine, con tool-calling realista); verifica la cita en BD;
    el reintento deja **1 sola cita** (guard anti-doble-agenda + idempotency
    key determinista del engine).
  - (3) Handoff: el stub emite `escalate_to_human` ante la urgencia (como
    haría un LLM real con el system prompt de la plantilla); se verifica el
    `Handoff` creado, `conversation.mode == "human"` y — con el **drenador
    real** — que el siguiente mensaje no recibe respuesta (silencio).
  - (4) Recordatorios: cita a +2 h + `scheduler.run_once(dry_run=True)` dos
    veces; genera `reminder_log` y la segunda corrida no duplica.
- **FAQ de apoyo**: FakeEmbedder es léxico (no semántico); la pregunta
  natural no alcanza el umbral 0.75 contra los chunks largos del seed
  (similitud ~0.25). La demo ingiere un FAQ corto ("¿Cuánto cuesta la
  consulta general? Cuesta $600 MXN.", similitud ~0.85) por el pipeline real.
  Con embeddings de OpenAI el seed solo bastaría. Documentado, no oculto.

## 2. Fix: `sensitive_keywords` vs `temas_sensibles` (engine)

Bug real encontrado al construir la demo: el engine leía
`tenant_configs.extra["sensitive_keywords"]`, pero el onboarding (Fase 4)
guarda la lista del perfil como `extra["temas_sensibles"]`. Resultado: los
tenants dados de alta por plantilla **jamás** disparaban la escalación
automática por palabra clave. Fix mínimo en `run_agent`: acepta ambas
claves (compatibilidad total; sin la clave no cambia nada). Test dedicado
en `tests/test_fase5.py`.

Limitación honesta que queda: el match es por substring de frase. Las
frases largas del perfil ("urgencias médicas o emergencias") rara vez
aparecen literales en un mensaje real; la vía principal de escalación para
urgencias redactadas con otras palabras sigue siendo el LLM (vía
`escalate_to_human`, que el system prompt de la plantilla ordena). Futuro:
derivar keywords cortas por plantilla o clasificación dedicada.

## 3. Fix: `scheduler.run_once` usa la zona horaria del tenant

Bug real expuesto por la demo: el scheduler comparaba `start_at` (naive en
hora **local** del tenant, convención del calendario) contra
`datetime.utcnow()` (naive UTC). En `America/Mexico_City` (UTC-6), una cita
a +2 h se veía como "hace 4 h" → **el recordatorio de 2 h jamás disparaba
para citas del mismo día**. Fix: `now` se calcula por tenant con
`ZoneInfo(tenant.timezone)` (fallback defensivo a UTC). Los tests de Fase 2
llaman a `load_rule_targets` con `now` explícito, así que no se rompieron
(verificado: 101 passed).

## 4. `app/worker.py` (nuevo)

- `python -m app.worker`: loop asyncio que drena `webhook_jobs`
  (reusa `drain_jobs` de Fase 1) y corre el scheduler de recordatorios
  (reusa `app/reminders/scheduler.py::run_once`). Intervalos por env
  (`WORKER_DRAIN_INTERVAL_S`, `WORKER_REMINDER_INTERVAL_S`,
  `WORKER_DRAIN_LIMIT`); apagado limpio con SIGTERM/SIGINT (termina el ciclo
  en curso, nunca a mitad de un job).
- `run_cycle()` es una función pura de un ciclo → testeable sin loop
  infinito (`test_worker_cycle_drains_pending_job`).
- **Dev vs prod**: `app/main.py` conserva el scheduler in-process en el
  lifespan (útil en dev); en producción se usa este worker separado. Si
  ambos corren a la vez, la idempotencia por `reminder_log` evita duplicados
  pero es desperdicio — documentado en el docstring del worker y en la guía.

## 5. `Makefile` (nuevo)

`up` (postgres → crea BD → `alembic upgrade head` → api + worker en
background con logs y pids en `logs/`), `down`, `migrate`, `test` (con el
comando de proxies), `onboard TENANT=... TEMPLATE=...`, `demo`,
`seed-admin`. Todo con mensajes de error accionables. El venv se resuelve
como `../.venv` si existe (layout de este workspace) o `.venv` si no; se
crea solo si falta. Sin Docker en esta VM: `up` verifica `pg_isready` y da
instrucciones si Postgres no responde.

## 6. `docs/GUIA_ALTA.md` (nueva)

Checklist 2–5 días: perfil → onboard → conocimiento → plantillas HSM de
Meta (**advertencia explícita**: aprobación de Meta = días/semanas
calendario, dependencia del cliente) → canal → pruebas (4 rutas) → entrega.
Matriz de costos (WhatsApp por conversación 24 h + OpenAI por tokens, ambos
a cargo del cliente) y alcance (incluido / cambio menor / nuevo desarrollo).

## 7. Limpieza

`logs/demo_evidence_*.txt` y `logs/demo_tests_extra_*.txt` movidos a
`../logs-archive/` (fuera del repo; artefactos de corrida, no código). Nada
los referencia (solo los scripts legacy que los generaban). `logs/.gitignore`
ignora `*.log`, `*.pid` y `*.txt` de runtime.

## 8. `.env.example` y `README.md`

- `.env.example`: documentadas todas las vars que el código usa
  (`OPENAI_API_KEY`, `EMBED_DIM`, `ENABLE_REMINDER_SCHEDULER`,
  `TEST_DATABASE_URL`, `LIAH_JWT_SECRET`, `LIAH_ADMIN_PASSWORD`,
  `LIAH_SEND_DRY_RUN`, defaults del factory LLM, `LIAH_MODEL_PRICES_JSON`,
  vars de Ollama, `LIAH_TENANT_ADMIN_PASSWORD`, y las nuevas
  `LIAH_DEMO_DATABASE_URL`, `LIAH_DEMO_RESET`, `WORKER_*`).
- `README.md`: estado final del esqueleto (5 fases), quickstart
  (`make up`, `make seed-admin`, `make onboard`, `make demo`), arquitectura
  en 10 líneas, links a `docs/`.

## Pendientes honestos (no son Fase 5)

- **Fase 6** (empaque comercial): Dockerfiles, `docker-compose` con
  api+worker+migrate, `uv.lock`, `docs/DEPLOY.md`, rate-limit/CORS/logging
  en `app/main.py`, quitar el scheduler in-process (dejarlo solo dev).
- **Seguridad**: RLS o defensa en profundidad del aislamiento (hoy 100%
  lógico por `tenant_id`); aviso de privacidad y flujo de revocación
  (LFPDPPP) en el panel; rotación/secret manager real para
  `token_secret_ref`.
- **Canales futuros**: Instagram/Facebook detrás de `ChannelAdapter`
  (el contrato ya existe; no implementados).
- **Verificación con Meta real**: aprobación de plantillas HSM y envío real
  fuera de dry-run no se han probado (requieren cuenta del cliente).
- **Plantillas por giro futuras** (ideas de Steve en memoria): escuela
  privada, academia de danza, barras de snacks para eventos, espejo mágico
  de fotos — la demo de Fase 5 ya prueba que el mecanismo es genérico.
