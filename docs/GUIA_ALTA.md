# Guía de alta de un cliente nuevo — Esqueleto Liah

Checklist repetible para dar de alta un negocio en **2 a 5 días hábiles de
ingeniería**. Los tiempos calendario pueden extenderse por dependencias del
cliente (marcadas con ⚠️).

## Plantillas de giro disponibles (Fase 5b)

El alta se hace eligiendo una plantilla en `templates/` (listadas también en
`/admin/onboard`). Negocios ficticios de ejemplo; el operador personaliza con
overrides sin tocar el YAML:

| Plantilla | Giro que cubre | Acción principal | Recordatorios |
|---|---|---|---|
| `consultorio_medico` | Consultorios / clínicas médicas | Agendar/cancelar/reprogramar citas | 24 h y 2 h antes (consentido) |
| `estetica` | Estéticas / salones de belleza | Agendar/cancelar citas de servicios | 24 h antes (consentido) |
| `escuela_privada` | Escuelas privadas | Agendar visitas informativas / recorridos | 24 h y 2 h antes (consentido); pago de colegiatura pendiente (fase 6) |
| `academia_danza` | Academias de danza / baile | Agendar clases de prueba gratuitas | 24 h antes (consentido); pago de mensualidad pendiente (fase 6) |

Todas fuerzan handoff a humano en temas sensibles definidos en
`politicas.temas_sensibles` (p. ej. urgencias médicas, bullying, lesiones,
menores sin tutor). Nuevo giro = nuevo YAML validado por
`app/core/profile_schema.py` (ver `tests/test_templates_giros.py`).

## Día 0 — Perfil del negocio (levantamiento, 2–4 h)

Levantar con el cliente (dueño o encargado):

- [ ] Nombre comercial, slug deseado, zona horaria, tono de atención
      (formal/cálido/etc.).
- [ ] Servicios con precios y duración; horarios por día; ubicación.
- [ ] Políticas: cancelación/reprogramación (anticipación mínima),
      qué hacer ante urgencias o temas sensibles (siempre → humano).
- [ ] Consentimiento de recordatorios: ¿el cliente ya pide permiso a sus
      pacientes? (LFPDPPP: sin `granted` no se envía nada proactivo).
- [ ] Entregables del cliente: aviso de privacidad publicado (obligatorio
      antes de captar datos), lista de servicios/precios en limpio.

## Día 0–1 — Alta técnica (onboard, 1–2 h)

- [ ] Elegir plantilla de giro en `templates/` (ver tabla arriba:
      `consultorio_medico`, `estetica`, `escuela_privada`, `academia_danza`).
- [ ] Preparar **overrides** (JSON) con lo propio del negocio: nombre,
      precios, horarios, tono. No editar el YAML de la plantilla.
- [ ] Alta (operador):
      `make onboard TENANT=clinica-ejemplo TEMPLATE=consultorio_medico`
      o desde el panel `/admin/onboard` (misma transacción).
      Guardar la **API key** (se muestra una sola vez) y el password del
      `tenant_admin` en el vault del operador.
- [ ] Verificar en el panel: tenant creado, reglas activas, plantillas HSM
      en estado `pending`, conocimiento semilla ingerido.

## Día 1–2 — Conocimiento (2–4 h)

- [ ] Subir documentos del negocio (panel o `POST /tenants/{id}/knowledge`):
      servicios, precios, políticas, preguntas frecuentes.
- [ ] Probar la ruta de conocimiento: preguntar precios/horarios y verificar
      que la **respuesta final** trae el dato (ver `make demo`, ruta 1).

## Día 1–3 — Plantillas HSM de Meta ⚠️ (dependencia del cliente)

- [ ] Crear en el panel de Meta **las mismas plantillas** que declara la
      plantilla del giro (`recordatorio_cita`, `confirmacion_cita`, etc.),
      con idéntico nombre y variables `{{1}}…{{n}}`.
- [ ] ⚠️ **La aprobación de Meta toma días o semanas calendario** y no la
      controla ingeniería: requiere la cuenta de WhatsApp Business del
      cliente verificada, nombre visible aprobado y número dedicado.
      Sin plantillas aprobadas NO hay mensajes proactivos (recordatorios):
      el bot solo conversa dentro de la ventana de 24 h.
- [ ] Cuando Meta las apruebe, marcarlas `approved` en el panel del tenant.

## Día 2–4 — Canal de WhatsApp (2–3 h + ⚠️ verificación)

- [ ] Conectar el número (Embedded Signup) o registrar `phone_number_id` +
      token en el canal del tenant (`token_secret_ref` → secret manager en
      producción; `WA_TOKEN_<ref>` en dev).
- [ ] Configurar en Meta la URL del webhook + `WHATSAPP_VERIFY_TOKEN`; probar
      el GET de verificación y un POST firmado (firma HMAC-SHA256).
- [ ] `LIAH_SEND_DRY_RUN=0` **solo** cuando el token real esté configurado;
      antes de eso todo envío es simulado (dry-run) por diseño.

## Día 3–5 — Pruebas de las 4 rutas (2–4 h)

- [ ] `make demo` en el tenant de staging: las 4 rutas en verde
      (`DEMO OK (4/4 rutas)`).
- [ ] Prueba manual por WhatsApp real con el número del cliente:
      1. Pregunta de precio/horario → dato correcto.
      2. Agendar → llega la confirmación; reintentar no duplica.
      3. Mensaje de urgencia → el bot escala y **guarda silencio**.
      4. Cita próxima → llega el recordatorio (requiere HSM aprobada +
         consentimiento `granted` del contacto de prueba).
- [ ] Revisar costeo en el panel: `usage_records` con tokens y `cost_usd`
      por conversación.

## Entrega (día 5)

- [ ] Credenciales entregadas al cliente: acceso `tenant_admin` al panel,
      API key (rotación disponible en el panel).
- [ ] Documento de costos firmado (ver matriz abajo): el cliente paga
      directo a Meta y a OpenAI; nosotros facturamos el tier.
- [ ] Aviso de privacidad del cliente publicado y ligado en el primer
      mensaje del bot (texto configurable por tenant).

## Matriz de costos (quién paga qué)

| Concepto | Quién lo paga | Notas |
|---|---|---|
| Conversaciones WhatsApp (ventana 24 h) | El cliente, directo a Meta | Precio por país/categoría (utility, marketing…); cambia sin aviso |
| Plantillas HSM aprobadas | El cliente (su cuenta Business) | Aprobación: días/semanas; fuera de nuestro control |
| Tokens OpenAI (chat) | El cliente (su API key por tenant) | Medido por turno en `usage_records`; visible en el panel |
| Embeddings OpenAI | El cliente (misma key) | Solo al ingerir/actualizar conocimiento |
| Número dedicado / verificación | El cliente | Requisito de Meta |
| Tier Liah (operador) | El cliente, a nosotros | Incluye alta, panel, soporte; margen sobre el costo medido |
| **Conversaciones de marketing (campañas/avisos)** | El cliente, directo a Meta | Categoría `marketing`: se cobra por conversación de 24 h iniciada por plantilla; cada envío se registra en `usage_records` (`kind=campaign`) con costo configurable (`LIAH_CAMPAIGN_COST_USD`, default 0.06 USD; calibrar con la matriz de Meta por país) |

Regla de oro: **el esqueleto mide todo** (tokens y conversaciones por
tenant) para que el margen del tier nunca se evapore en silencio.

## Campañas y avisos (Fase 6) — política de opt-in y operación

**Sin opt-in no hay envío. Punto.** Ni promociones ni avisos
institucionales (suspensión de clases, cambios de horario): un HSM no
solicitado es spam y Meta banea el número del negocio. El ban no es un
"error que se corrige": se pierde el canal.

- [ ] **Captura de opt-in**: (a) por palabra clave en conversación
      ("quiero recibir promociones" → opt-in; "baja" → opt-out inmediato),
      (b) toggle en el panel (fuente `panel`, operador identificado),
      (c) importación masiva con evidencia (fuente `import`: el cliente
      entrega la prueba del consentimiento — lista firmada, formulario).
      El opt-in por keyword queda auditado en `event_log`.
- [ ] **Plantillas de campaña aprobadas**: crear la plantilla de marketing
      en el Business Manager (categoría `marketing`), esperar la aprobación
      de Meta (días/semanas, dependencia del cliente) y marcarla `approved`
      en el panel (Campañas → Plantillas). El lanzamiento exige plantilla
      aprobada: sin eso, 422 y la campaña no sale de borrador.
- [ ] **Flujo de lanzamiento**: crear en `/admin/campaigns` (tipo, plantilla
      aprobada, segmento, variables, programar) → **estimar destinatarios**
      (aplica segmento + opt-in; revisarlo antes de lanzar) → lanzar. El
      worker envía con pacing (default 1 msg/seg por tenant, configurable).
- [ ] **Segmentación**: `prospect` (llegó por el canal) vs `client` (ya
      agendó; el sistema lo promueve solo al confirmar la cita). Tags libres
      por contacto (`vip`, `pediatria`…) con OR entre ellos.
- [ ] **Métricas post-campaña**: enviados, entregados, leídos, tasa de
      lectura (leídos/entregados) y costo total en el detalle de la campaña.
      Los `statuses` de Meta (delivered/read) se matchean por `wamid`.

**Alcance comercial**: las campañas son feature de tier superior (el
cliente paga las conversaciones de marketing a Meta + el tier). No
prometer "envío masivo gratis": cada mensaje tiene costo y riesgo de baneo
si se abusa.

## Alcance: incluido vs. cambio menor vs. nuevo desarrollo

**Incluido en el alta estándar**
- Alta por plantilla de giro (sin tocar código), conocimiento semilla,
  las 4 rutas (conocimiento, agendar/cancelar/reprogramar, handoff,
  recordatorios 24 h/2 h), panel (bandeja de handoff, config por tenant,
  métricas y costo por conversación).

**Cambio menor** (horas, se cotiza por evento)
- Ajustar tono/textos, horarios, servicios y precios en el KB, nuevas
  plantillas HSM (con su aprobación de Meta), reglas con parámetros ya
  existentes, preguntas frecuentes adicionales.

**Nuevo desarrollo** (días/semanas, se cotiza como proyecto)
- Nuevo tipo de regla de automatización, nuevo canal (Instagram/Facebook),
  integraciones (CRM, pagos, agenda externa), reportes a medida,
  aislamiento reforzado (RLS/esquemas por tenant), cambios al motor.

## Notas operativas

- Staging separado de producción: el `make demo` corre contra
  `pyme_agent_demo`; nunca contra la BD del cliente.
- `make up` levanta api + worker; en producción el worker corre en su
  propio proceso/servicio (`python -m app.worker`), no in-process.
- Ante una caída a mitad de un webhook: el job queda `pending`/`failed` en
  BD y se reprocesa sin duplicar (idempotencia por `wamid` + action_log).
