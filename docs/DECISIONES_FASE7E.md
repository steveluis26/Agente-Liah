# DECISIONES — Fase 7e: verificación integral + tests de borde + demo extendida

Fecha: 2026-09-21. Rama `skeleton`. Sin commit (cambios en working tree).

## 1. Verificación

- **Suite completa: 208/208 en verde** (198 preexistentes + 10 nuevos de borde).
  Baseline antes de tocar nada: 198/198. Después de los cambios: 208/208.
- **`alembic upgrade head` corre limpio** sobre la BD de test
  (`pyme_agent_test`, postgres 127.0.0.1:5433): todas las migraciones hasta
  `f7f_staff` se aplican sin error. `migrations/` no se tocó (solo
  verificación, como pedía la tarea).
- **Las 4 demos corren en verde y son repetibles sin `--reset`**:
  - `demo_consultorio.py` (Fase 5, intacta): 4/4 rutas.
  - `demo_consultorio_recursos.py` (nueva): 5/5.
  - `demo_espejo_magico.py` (nueva): 4/4.
  - `demo_staff.py` (nueva): 5/5.
  - `make demo` ahora corre las 4 en secuencia; cada una tiene su target
    propio (`demo-consultorio`, `demo-recursos`, `demo-espejo`, `demo-staff`).

## 2. Fixes de integración (mínimos, documentados)

### 2a. `app/agent/staff.py::_cmd_free` — `resource_id` huérfano
**`app/agent/staff.py::_cmd_free` — `resource_id` huérfano.** Al revisar el
borde "specialist sin recurso asignado" se encontró que un `resource_id`
que ya no existe en `resources` hacía que el especialista viera los huecos
de TODOS los tipos de servicio (sobre-permiso silencioso), en vez de
degradarse como el caso `resource_id=None`.

Investigación: el FK `staff_members.resource_id` es `ondelete="SET NULL"`,
así que borrar un recurso por el panel deja `resource_id=NULL` (caso ya
cubierto). El `resource_id` huérfano no-NULL solo es alcanzable por
manipulación directa de BD, pero el fix es de 5 líneas y cierra el hueco:
si `session.get(Resource, ...)` devuelve `None`, se responde el mismo
mensaje claro de "sin recurso válido asignado" en vez de listar todo.

```python
resource = await session.get(Resource, staff.resource_id)
if resource is None:
    return (
        "Tu rol de especialista no tiene un recurso válido asignado; "
        "pídele al administrador que lo revise."
    )
```

### 2b. `scripts/demo_consultorio.py` vs. puerta de consentimiento (Fase 7c)

La demo de Fase 5 (anterior a la puerta de privacidad) fallaba 2/4 rutas
al correrla contra el código actual:

1. **Ruta booking**: el helper `_get_or_create_contact` creaba el contacto
   con `consent_status="granted"` pero sin `privacy_terms_version` →
   `consent_is_valid` es falso → el tool `book_appointment` rechaza la
   agenda. Fix: al otorgar `granted` se fija la versión vigente de
   `TenantPrivacyTerms` (y se auto-repara en contactos legacy ya creados).
2. **Ruta handoff (silencio)**: el paso 3b crea un contacto nuevo por el
   drenador con consent `none` → la puerta envía el aviso de privacidad en
   vez de escalar, y el "silencio tras handoff" nunca se prueba. Fix: la
   demo da de alta el contacto con consentimiento ANTES de encolar el
   mensaje (igual que ya hacía el paso 3a).

### 2c. `scripts/demo_consultorio.py` — re-ejecución en el mismo minuto

La ruta de recordatorios reserva "ahora + 2h" truncado al minuto y nunca
limpiaba sus citas: dos corridas dentro del mismo minuto colisionaban en el
índice único `(tenant_id, start_at)` ("slot ocupado"). Fix: la ruta limpia
sus propios artefactos antes de reservar (mismo patrón que `_cleanup_route2`).

No se cambió ninguna otra lógica de negocio de fases anteriores.

## 3. Tests de borde (`tests/test_fase7e_bordes.py`, 10 tests)

Solo casos SIN cobertura previa (se revisaron `test_fase7c_*`, `test_fase7d_*`,
`test_fase7f_*` para no duplicar: matcheo de zonas con acentos/mayúsculas,
waitlist al primer candidato que califica, capacidad 0 → 422 en el panel y
scoping básico de specialist ya estaban cubiertos):

1. `test_mobile_venue_none_vs_none_no_travel` — negocio móvil, ambos venues
   `None` → sin costo de traslado (misma sede).
2. `test_mobile_venue_none_vs_declared_uses_default` — vecino sin venue +
   candidato con venue → se aplica la política `per_zone` (conflicto `travel`
   puro, sin conflicto de capacidad).
3. `test_mobile_venue_none_feasible_gap_accepted` — con hueco suficiente el
   traslado contra venue `None` sí cabe.
4. `test_double_consent_idempotent` — re-aceptar la versión vigente no vuelve
   a `pending`, no cambia la versión, la puerta queda abierta.
5. `test_consent_reaccept_does_not_revoke` — un mensaje cualquiera de un
   contacto `granted` no lo revoca.
6. `test_waitlist_skips_non_qualifying_first_entry` — dos candidatas: la
   primera NO califica (traslape de su propia agenda) → la oferta va a la
   segunda; la primera sigue `waiting`; un solo aviso.
7-9. `test_specialist_without_resource_{agenda,who_at,free_slots}` — specialist
   sin `resource_id`: los tres comandos responden con mensaje claro, sin crash.
10. `test_specialist_resource_deleted_degrades_gracefully` — borrar el recurso
    del specialist (SET NULL) degrada igual que "sin asignar".

## 4. Demos

Patrón heredado de `demo_consultorio.py`: BD propia por demo (se crea si no
existe; nunca la BD de test), onboarding REAL, veredictos sobre resultados
reales, sin `drop_all` sin `--reset`, exit code != 0 si algo falla. La infra
común vive en `scripts/demo_lib.py` (nuevo).

| Demo | BD | Qué demuestra |
|---|---|---|
| `scripts/demo_consultorio.py` | `pyme_agent_demo` | Sin cambios de contrato; se le aplicaron los fixes 2b/2c (puerta de consentimiento + limpieza de re-ejecución). 4/4 rutas. |
| `scripts/demo_consultorio_recursos.py` | `pyme_agent_demo_recursos` | Cita con `service_type`: dos citas no traslapan el mismo consultorio/especialista; buffers setup/teardown; la puerta de privacidad se pide en el primer contacto antes de agendar (puerta cerrada → aviso → "sí" → granted v1.0 → agenda OK). |
| `scripts/demo_espejo_magico.py` | `pyme_agent_demo_espejo` | Negocio móvil: evento 16:00–20:00 en "Xalapa" OK (muestra buffers y el intervalo ocupado [15:15, 19:30]); mismo día 19:00 en "Veracruz" RECHAZADO (recurso ocupado); mismo día 20:30 RECHAZADO por TRASLADO imposible con veredicto explícito ("se necesitan 60 min entre 'Xalapa' y 'Veracruz', hay 15 min"); al día siguiente OK. |
| `scripts/demo_staff.py` | `pyme_agent_demo_staff` | "mi agenda de hoy", "¿quién es el de las 10:30?", "¿qué huecos hay mañana?" con datos reales; scoping por rol (la especialista solo ve su cita); resumen matutino con `staff_briefing` habilitado, verificado en los mensajes outbound persistidos. |

Cómo correr:

```bash
make demo            # las 4 demos en secuencia
make demo-consultorio | make demo-recursos | make demo-espejo | make demo-staff
DEMO_ARGS=--reset make demo-espejo   # empezar de cero una demo
```

## 5. Pendientes honestos detectados (no bloquean, no se tocaron)

- **Índice único `(tenant_id, start_at)` en citas** (ya documentado en
  `docs/DECISIONES_FASE7C.md`): dos citas no pueden compartir el instante
  exacto aunque usen recursos distintos. En la demo de recursos se evitó
  usando horarios distintos; relajarlo requiere migración.
- **Asignación determinista de recursos por tipo** (`_resolve_resources` toma
  los primeros N por slug, no "el primero libre"): si el consultorio-1 está
  ocupado, pediatría a esa hora falla aunque el consultorio-2 esté libre. Es
  el diseño actual (determinista y testeado); una asignación "best-fit" sería
  un cambio de producto, no un fix.
- **`_cmd_free` no filtra recursos por tenant** (`session.get(Resource, id)`
  sin scope): un `resource_id` de otro tenant se resolvería. Hoy el panel
  solo permite asignar recursos del propio tenant, así que no es alcanzable
  por la UI; se deja anotado para endurecer junto con el resto del scoping.
- Las demos crean BDs `pyme_agent_demo_*` nuevas en el postgres local; si se
  quiere limpiar, borrarlas a mano (`dropdb`).
