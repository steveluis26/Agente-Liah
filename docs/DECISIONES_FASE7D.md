# DECISIONES — Fase 7d: CRM/panel (recursos, configuración instalada, consentimientos, segmentación)

Fecha: 2026-09-21. Rama `skeleton`, sin commit (working tree).

## Lo que se construyó

**1. Gestión de recursos** — API `GET/POST/PUT/DELETE /api/v1/admin/tenants/{tenant_id}/resources`
(`app/api/admin.py`, sección "Recursos (Fase 7d)") + página `/admin/recursos`
(`app/api/admin_ui.py` + `app/templates/recursos.html`).

**2. Vista de configuración instalada** — API `GET /api/v1/admin/tenants/{tenant_id}/installed-config`
+ página `/admin/configuracion` (solo lectura).

**3. Consentimientos visibles** — `GET /tenants/{id}/contacts` ahora devuelve
`consent_status`, `consent_at`, `privacy_terms_version` por contacto; la página
`/admin/contacts` los muestra y agrega filtro por `contact_type`.

**4. Segmentación curioso/cliente** — ya existía el badge `contact_type`
(Fase 6); se agregó el **filtro** `Todos / Curiosos (prospect) / Clientes (client)`
en la página (no se duplicó el badge).

## Decisiones

1. **Rutas anidadas, no `/api/v1/admin/resources` planas.** El task pedía
   `GET/POST/PUT/DELETE /api/v1/admin/resources`, pero todo el API existente
   usa `/api/v1/admin/tenants/{tenant_id}/...` (scope por path + `require_tenant_access`).
   Se siguió la convención existente: multi-tenant por construcción.

2. **Roles: solo platform_admin + tenant_admin gestionan recursos**
   (`RESOURCE_ADMIN_ROLES`). tenant_agent puede operar la bandeja y ver contactos,
   pero no define qué puede reservar la agenda (definir recursos = definir oferta
   del negocio). Para `installed-config` se usaron los mismos roles que el editor
   de config (`get_tenant_config`).

3. **Borrado bloqueado con 409, nunca en cascada silencioso.** `DELETE resource`
   cuenta citas **futuras no canceladas** que lo usan (vía `appointment_resources`
   ⋈ `appointments.start_at >= now AND status != 'cancelled'`) y responde 409 con
   el conteo y la instrucción accionable ("cancela o reasigna esas citas primero").
   Las citas pasadas y las canceladas no bloquean. El FK tiene `ondelete=CASCADE`;
   el check previo evita que se dispare en silencio. Se audita con
   `resource.created/updated/deleted` en `log_event`, como el resto del panel.

4. **Validación pydantic estricta, cero literales de vertical.** `tipo` se valida
   contra `RESOURCE_TYPES` y `movilidad` contra `RESOURCE_MOBILITY` del propio
   modelo (room|specialist|equipment|staff, fixed|mobile); el slug usa el mismo
   regex que el onboarding; `capacidad >= 1`; unicidad de slug **por tenant**
   (409, verificado en código antes de insertar).

5. **Configuración instalada = snapshot de solo lectura.** `installed-config`
   junta tenant, `extra.template` + `extra.template_schema_version` + `giro`
   (lo que escribió el onboarding), business_hours, resources, service_types
   (duración, recursos_requeridos, buffers, traslado) y el aviso de privacidad
   vigente (versión + título + texto). La página dice explícito que es la
   **una sola fuente de verdad** que usan el chatbot y el CRM, y no tiene
   edición: editarla aquí sería una segunda fuente. Se verificó contra un
   onboarding real (`onboard_tenant` + `FakeEmbedder`, plantilla
   `consultorio_medico`): refleja `template`, schema `1.1`, los recursos del
   perfil, los tipos de servicio con buffers/traslado y privacy v1.0.

6. **Consentimientos en el listado de contactos (no en página aparte).** Ya
   existía `/admin/contacts`; se extendió con columnas Consentimiento
   (aceptado/pendiente/revocado/sin registro + fecha), Aviso de privacidad
   (versión aceptada) y el filtro de tipo. El API ya soportaba `contact_type`;
   la página no lo pasaba — ahora sí.

## Tests

`tests/test_fase7d_panel.py` — 9 tests:
- CRUD completo de recursos (crear/listar/editar parcial/eliminar).
- Validación: tipo/movilidad/slug/capacidad inválidos → 422; slug duplicado
  (POST y PUT) → 409; recurso inexistente → 404.
- Roles y scope: tenant_agent → 403; tenant_admin en otro tenant → 403;
  platform_admin gestiona en ambos; unicidad de slug por tenant.
- Borrado bloqueado: 409 con 1 cita futura confirmada (la cancelada y la
  pasada no cuentan); el recurso sobrevive; tras cancelar, el DELETE procede.
- `installed-config` refleja el onboarding real (plantilla, schema 1.1, giro,
  recursos, service_types con buffers/traslado, privacy v1.0, horarios).
- Roles de installed-config (tenant_agent → 403).
- Consentimientos visibles por contacto + filtro `contact_type=client/prospect`
  (válido) e inválido → 422.
- UI: `/admin/recursos` y `/admin/configuracion` (redirect sin sesión; 200 con
  cookie; el recurso creado por API aparece en la página), y `/admin/contacts`
  muestra consentimiento + filtro prospect seleccionado.

## Huecos vistos (no cubiertos en esta fase)

- **Sin edición de recursos en conflicto con citas existentes:** cambiar el
  `tipo` o `movilidad` de un recurso con citas futuras es posible hoy; el
  anti-traslape real de recursos lo hará el engine de agenda (Fase 7c está en
  curso en paralelo sobre `app/agent/*`).
- **`installed-config` no incluye `model_routing` ni `automation_rules`:**
  `model_routing` ya es editable en `/admin/config`; las reglas de
  automatización no tienen vista en el panel. Candidato natural para una fase
  7e ("configuración instalada completa").
- **Sin paginación en `/resources`:** el listado no pagina (el de contactos
  limita a 200). Un tenant con cientos de recursos pagaría el listado completo;
  acceptable por ahora (los recursos son decenas, no miles).
- **Sin edición del aviso de privacidad ni de tipos de servicio en el panel:**
  hoy solo se instalan vía onboarding/levantamiento. Si el operador necesita
  corregir un tipo de servicio, tiene que hacerlo en BD o re-onboard.
- **La página de contactos no filtra por `consent_status`:** el API tampoco
  lo soporta (filtros actuales: `opt_in`, `contact_type`, `q`). Fácil de agregar
  si el CRM lo pide.
