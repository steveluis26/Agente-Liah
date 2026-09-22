# Decisiones Fase 3 — Panel mínimo (2026-09-21)

Panel de operador: API JSON (`/api/v1/admin`) + UI mínima server-rendered
(`/admin`, Jinja2, sin build step). Bandeja de handoff, config por tenant y
métricas/costo.

## Modelo `conversations`

Tabla `conversations` (tenant, contacto, canal, `mode: ai|human|resolved`,
`opened_at`, `closed_at`). Un "episodio" por contacto:

- El drenador la crea o reutiliza por contacto (`get_or_create_conversation`).
- El handoff la pone en `human` (en `engine._create_handoff` y en la tool
  `escalate_to_human`); el drenador silencia al bot cuando `mode == "human"`.
  **Fase 1 silenciaba por handoff abierto; Fase 3 mueve la única fuente de
  verdad al modo de la conversación** (el handoff y el modo siempre se
  actualizan juntos).
- `POST /handoffs/{id}/return-to-bot` → `mode = ai` (el drenador vuelve a
  responder), handoff cerrado.
- `POST /handoffs/{id}/resolve` (con nota) → `mode = resolved`,
  `closed_at = now()`: el episodio queda cerrado para métricas.
- Un episodio `resolved` **no se reutiliza**: el próximo inbound abre uno
  nuevo en `ai`. Así "resolución automática %" cuadra por episodio
  (cerradas sin handoff / total de cerradas).
- `channel` hoy es `whatsapp`; Instagram/Facebook futuros usan el mismo
  modelo vía `ChannelAdapter` (sin cambios de esquema).
- `run_agent(..., conversation_id=...)`: el costeo por turno (Fase 2) ahora
  enlaza `usage_records.conversation_id`, cerrando el pendiente que Fase 2
  dejó explícito ("nullable hasta que fase 3 modele conversación").

## Auth humana: JWT HS256 con stdlib + PBKDF2

- **JWT implementado a mano** (`hmac` + `base64url` de la stdlib), sin
  agregar PyJWT: el esqueleto minimiza dependencias y el uso es acotado
  (un secreto simétrico, un emisor). Reglas no negociables: solo se acepta
  `alg: HS256` (falla cerrado ante alg-confusion), se verifican firma
  (compare_digest), `exp`, `iat`, y que el `sub`/`role`/`tenant_id` sigan
  vigentes en BD (usuario borrado o con rol cambiado invalida el token).
- Secreto: `LIAH_JWT_SECRET` (env); **rechazo de defaults en producción**
  (entra al validador `_forbid_default_secrets_in_production` de config).
  Expiración configurable: `LIAH_JWT_EXPIRE_MINUTES` (default 480 = 8h,
  una jornada de operador).
- Transporte dual, misma verificación: header `Authorization: Bearer`
  (clientes API) o cookie httpOnly `liah_admin_token` (UI `/admin`; la fija
  el login, `Secure` solo en prod, `SameSite=Lax`).
- **Passwords con `hashlib.pbkdf2_hmac` (SHA-256, 600k iteraciones, salt de
  16 bytes por usuario)**, formato
  `pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>`. Por qué no bcrypt/argon2
  como dependencia: el esqueleto evita dependencias nuevas cuando la stdlib
  cubre el caso; 600k iteraciones es el mínimo recomendado por OWASP para
  PBKDF2-HMAC-SHA256. Si el threat model sube, migrar a argon2id es un
  cambio localizado (el formato lleva el algoritmo).
- Roles: `platform_admin` (todo; `tenant_id` NULL), `tenant_admin`
  (config+bandeja+métricas de su tenant), `tenant_agent` (bandeja de su
  tenant). Dependencias: `get_current_user`, `require_platform_admin`,
  `require_tenant_role(...)`, y `require_tenant_access(tenant_id, user)`
  para el scope por ruta. Las API keys por tenant (`X-Tenant-API-Key`)
  **siguen vivas** para webhook/ingesta; no sirven para el panel.

## Endpoints cerrados en Fase 3

- `POST /tenants` ahora exige JWT de `platform_admin` (antes abierto a
  cualquiera: cualquiera podía crear tenants y llevarse API keys). La fase 4
  lo expondrá en el panel como "nuevo cliente desde plantilla".
- Callback de Embedded Signup: antes tomaba `tenant_id` del body sin auth
  (vector de hijack: enlazar tu WABA al tenant de otro). **Mecanismo
  elegido**: se acepta (1) JWT de `platform_admin` — el operador white-label
  completa el signup desde el panel — o (2) `X-Tenant-API-Key` **del mismo
  tenant del body** — el tenant enlaza su propio WhatsApp (self-service).
  Se descartó firmar el callback con el appsecret de Meta porque Meta no
  firma ese POST (lo origina el navegador del operador/tenant, no Meta);
  exigir firma habría sido teatro de seguridad.

## Config por tenant: validación estricta, cero secretos

- `PUT /tenants/{id}/config` valida `model_routing` con schema pydantic
  `extra="forbid"` contra **exactamente** lo que consume el factory de Fase
  2 (`llm_provider: openai|ollama`, `llm_model`, `llm_max_tokens: 1..100000`,
  `llm_temperature: 0..2`, `embedder: openai|ollama|fake`, `ollama_*`, `tier`).
  Clave desconocida → 422 (no se guarda en silencio). `business_hours` se
  valida como `{día: {open, close: HH:MM} | "closed"}`.
- `model_routing` enviado **reemplaza** el dict completo (documentado en el
  endpoint); el resto de campos son parciales.
- **El GET jamás devuelve secretos**: `TenantConfig` no tiene campos de
  secretos por diseño (API keys hasheadas en `tenants`, tokens como
  `secret_ref`); el endpoint lo documenta como invariante a mantener si el
  modelo crece. Los tests barren la respuesta buscando `api_key|secret|
  token|password`.

## Métricas

`GET /tenants/{id}/metrics?days=30` (cohorte = conversaciones abiertas en la
ventana):

- `auto_resolution_pct` = cerradas sin handoff / cerradas × 100. Un handoff
  se atribuye a la conversación si cae dentro de `[opened_at, closed_at]`
  del contacto (atribución por episodio, no por contacto global).
- `transfers` = handoffs creados en la ventana.
- `avg_first_response_seconds` = de `messages`: primer inbound → primer
  outbound posterior, por conversación (promedio de las medibles).
- `successful_actions` = `action_log` con `status='ok'` agrupado por acción.
- `cost_usd` = suma de `usage_records` en la ventana (tokens in/out,
  `per_conversation` sobre conversaciones con costo) + `monthly_total` de
  `usage_monthly` del mes en curso.

## UI mínima (`/admin`)

Decisión tomada en el task: API + UI mínima server-rendered, sin build step.
Jinja2 (nueva dependencia declarada en `pyproject.toml`). Páginas: login,
bandeja (ver/tomar/resolver/devolver), editor de config por tenant, tablero
de métricas/costo. Sin framework JS: formularios + `fetch` contra la API
real con `credentials: "include"` (la cookie httpOnly autentica). La UI no
tiene lógica de negocio propia: las páginas llaman a las mismas funciones
del router JSON (comparten scope y validación).

## Seed del operador inicial

`scripts/seed_platform_admin.py`: crea el primer `platform_admin` **solo si
`platform_users` está vacía** (nunca duplica ni resetea passwords). Password
por `LIAH_ADMIN_PASSWORD` o prompt interactivo (`getpass`); **jamás hay
default** — en no-interactivo sin la variable, falla con mensaje claro en
vez de inventar una credencial. Mínimo 12 caracteres en modo interactivo.

## Migración `f3_panel`

`conversations` nueva; `platform_users`: `password_hash` (nullable para no
romper filas existentes), `tenant_id` (FK, NULL = plataforma), y migración
de datos del rol legacy `'owner'` → `'platform_admin'`; `handoffs`:
`resolution_note`. Los tests siguen usando `create_all` (la migración es
para deploys reales).

## Pendientes conocidos (no de esta fase)

- Rate-limit/CORS/logging en el panel → Fase 6 (está en el CHANGE_MAP).
- `POST /tenants` crea el tenant pero no operador inicial del tenant: la
  fase 4 (alta desde plantilla en el panel) deberá crear el `tenant_admin`
  junto con el tenant.
- Sin flujo "olvidé mi password" ni rotación de JWT (revocación hoy = borrar
  o cambiar el rol del usuario).
- RLS sigue diferido (decisión Fase 2, vigente).
