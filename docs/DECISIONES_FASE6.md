# DECISIONES FASE 6 — Módulo de campañas y avisos

2026-09-21. Rama `skeleton`. Worker de esta fase.

## Reglas duras (no negociables, con motivo)

1. **Sin opt-in no hay campaña ni aviso.** El dispatch filtra por
   `contacts.marketing_opt_in=true` en CADA envío, no por snapshot al
   lanzar. Los avisos institucionales (suspensión de clases, cambios de
   horario) también lo exigen: un HSM no solicitado es spam y Meta banea
   el número del negocio (ban = se pierde el canal de TODOS los clientes
   del tenant). El opt-out es irreversible por campaña: un "baja" posterior
   al lanzamiento excluye al contacto automáticamente.
2. **Solo plantillas aprobadas por Meta.** El launch valida
   `templates.status='approved'`; si no, 422 con mensaje accionable y la
   campaña sigue en draft. El estado se refleja en el panel tras la
   aprobación en el Business Manager (días/semanas calendario, dependencia
   del cliente — ver GUIA_ALTA.md).
3. **Jamás duplicar.** `unique(campaign_id, contact_id)` + INSERT ON
   CONFLICT DO NOTHING + `idempotency_key=campaign:<id>:<contact_id>` en el
   sender (action_log). Re-lanzar o re-correr el dispatch es seguro.
4. **Pacing anti-baneo.** Default 1 msg/seg por tenant
   (`LIAH_CAMPAIGN_RATE_PER_SEC`), sobreescribible por tenant en
   `tenant_configs.extra["campaign_msgs_per_sec"]`. El dispatch corre en el
   ciclo del worker, nunca en el request. Conservador a propósito: un ban
   cuesta más que una campaña lenta.

## Diseño de segmentación

- `contacts.contact_type`: `prospect` por default (primer mensaje del
  canal). Pasa a `client` cuando se confirma un `book_appointment` (hook en
  el engine, post-tool result ok=true, vía `mark_contact_client()`).
  Criterio documentado aquí y no en otro lado: "conversión = el contacto
  ya generó valor medible para el negocio (cita agendada)". A futuro, pagos
  o compras llamarán la misma función; no se inventó ningún evento que no
  exista.
- `contact_tags`: tags libres por contacto, único por
  (tenant, contacto, tag). En el segmento, los tags se evalúan con **OR**
  (basta uno): `{"contact_type": "client", "tags": ["vip","moroso"]}` =
  clientes que sean vip O morosos. Elegido OR porque los tags suelen ser
  etiquetas alternativas, no acumulativas; documentado en la UI y en el
  docstring.
- La estimación de destinatarios (`POST .../estimate`) aplica segmento +
  opt-in ANTES del launch: es el paso que evita sorpresas de alcance.

## Opt-in por palabra clave

- Regla determinista en el drenador (`queue.py`), ANTES del agente, sin
  LLM: si el mensaje es un opt-in/opt-out, se procesa y se responde con
  texto enlatado (el agente no interviene ese turno). Motivo: el
  consentimiento no puede depender de que un modelo "entienda".
- Coincidencia por límites de palabra sobre texto normalizado (minúsculas,
  sin acentos, puntuación→espacios): "baja" no dispara dentro de "trabajan".
  Opt-out gana en empate ("quiero darme de baja").
- Listas configurables por tenant en `tenant_configs.extra`
  (`marketing_optin_keywords` / `marketing_optout_keywords`); defaults en
  español mexicano en `app/marketing/optin.py`.
- Fuentes de opt-in: `keyword` (drenador), `panel` (toggle con operador
  identificado), `import` (carga masiva con evidencia), `onboarding`
  (alta). El endpoint de panel rechaza `source=keyword` (esa la pone solo
  el drenador).

## Envío, statuses y costo

- El envío usa el sender existente (`send_template`, HSM): no se inventó
  ningún camino de envío nuevo. En dry-run se registra el outbound sin
  llamar a Meta (sin wamid: no hay statuses que matchear).
- Los `statuses` del webhook ahora también actualizan `campaign_sends`
  (delivered/read/failed por wamid), SIN degradar (un status tardío no
  baja read→delivered; un failed no pisa un delivered/read ya registrado).
  Los wamid que no son de campaña se ignoran (pertenecen a recordatorios u
  otros envíos).
- Costo: cada envío exitoso escribe `usage_records(kind='campaign',
  model='whatsapp_marketing', campaign_id, cost_usd)`. El costo por
  conversación es configurable (`LIAH_CAMPAIGN_COST_USD`, default 0.06 USD)
  y por tenant (`extra["campaign_cost_usd"]`). El default es un placeholder
  calibrable con la matriz de precios de Meta (varía por país/categoría);
  está documentado como tal, no como precio real. En dry-run el costo se
  registra como estimado (no hubo cargo real).
- Tasa de lectura = read / delivered (los no entregados no pudieron
  leerse); None si delivered=0.

## Panel

- `/admin/campaigns`: crear (tipo, plantilla aprobada, segmento, variables,
  programar), estimar antes de lanzar, lanzar/cancelar, detalle con
  métricas y muestra de envíos. Crear/lanzar/cancelar y el toggle de
  opt-in exigen `platform_admin` o `tenant_admin` (el dinero de Meta y el
  riesgo de baneo lo decide quien opera el negocio); `tenant_agent` recibe
  403 (testeado).
- `/admin/contacts`: opt-in visible por contacto, toggle con fuente,
  tags (normalizados a minúsculas_con_guiones_bajos).
- `PATCH .../templates/{id}/status`: refleja la aprobación de Meta
  (pending|approved|rejected, con CHECK en BD).

## Lo que NO se hizo (huecos honestos)

1. **Programación con zona horaria del tenant**: `scheduled_at` se guarda
   naive (convención del esquema: UTC). La UI pide ISO sin zona; el
   operador debe ingresarla en UTC. Pendiente: parseo con zona del tenant.
2. **Variables de plantilla sin validación contra `templates.variables`**:
   el launch no verifica que `params` cubra todas las variables de la
   plantilla; Meta rechazaría el envío y quedaría como `failed` con el
   error en el log. Pendiente: validación en `create_campaign`.
3. **Sin preview del mensaje final**: el panel no muestra el HSM renderizado
   con las variables antes de lanzar. Pendiente menor de UI.
4. **Import masivo con evidencia** (`source=import`): el endpoint de toggle
   existe, pero no hay CSV/bulk ni almacenamiento de la evidencia (archivo
   firmado, screenshot). El campo `source` está listo; el flujo de carga,
   no.
5. **Migración aplicada solo con --sql**: la BD de dev (5432) no estaba
   levantada en esta VM; `f6_campaigns` se validó con `alembic upgrade
   --sql` y con `Base.metadata.create_all()` en tests. Aplicar con
   `make migrate` en el próximo `make up`.

## Verificación

- `pytest`: 131 passed (baseline 109 + 22 de `tests/test_fase6.py`), 0 fallos.
- `alembic -c migrations/alembic.ini upgrade f4_onboarding:f6_campaigns --sql`:
  SQL válido (tablas + constraints + CHECKs).
- Sin secretos en código ni docs; datos de test solo en la BD de test.
