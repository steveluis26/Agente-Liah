# Decisiones de diseño — Fase 7b: esquema de configuración

Esquema de configuración declarativa (schema 1.1): recursos, tipos de
servicio y términos de privacidad. Código en `app/core/profile_schema.py`
(pydantic estricto), modelos en `app/models/`, migración
`migrations/versions/f7_scheduling.py`.

## 1. Recursos: el vocabulario mínimo de la agenda

Un `Recurso` es cualquier cosa que un servicio necesita: `room`
(sala/cabina/aula), `specialist` (médico, estilista, instructor),
`equipment` (electrocardiógrafo, barra de esquites, espejo mágico) o
`staff` (operador, ayudante, asesor). Cuatro tipos fueron suficientes
para cubrir los 6 giros sin literales de vertical en el código genérico.

`movilidad: fixed|mobile` es el campo que distingue el consultorio fijo
("tiene un local y no se mueve") de los negocios móviles de eventos: el
recurso viaja con el servicio. Esto venía del refinamiento de Steve
(2026-09-22): la disponibilidad de negocios móviles debe contemplar
servicio + traslado entre eventos.

## 2. Tipos de servicio: requerimiento por referencia o por tipo

Un `TipoServicio` declara duración, buffers (setup/teardown) y traslado.
Los recursos requeridos se expresan de dos formas excluyentes:

- `{"recurso": "<slug>"}`: referencia concreta (el "Dr. Pérez" específico,
  la barra de esquites #1). Validado contra los slugs de `resources` —
  un typo en el YAML es error en `load_template`, no un 500 en producción.
- `{"tipo": <tipo>, "cantidad": N, "especialidad": ...}`: requerimiento
  genérico ("cualquier sala", "2 operadores"). La especialidad refina
  (`specialist` de "pediatría", "hip-hop").

La discriminación es estricta (`extra="forbid"` + validador): ambas
claves juntas o ninguna es error. No se permite `cantidad` junto a
`recurso` (la capacidad vive en el recurso).

`service_types.recursos_requeridos` en BD es un **snapshot JSONB** del
perfil, no una FK: si el operador cambia el perfil después, las citas ya
hechas conservan lo que se cotizó.

## 3. Traslado: tres modos, sin magia

- `none`: negocio fijo o sin costo de tiempo.
- `fixed`: `fixed_min` requerido (> 0).
- `per_zone`: `zonas` no vacío (minutos por zona, ≥ 0); `default_min`
  (default 60) para zonas no listadas.

Las plantillas móviles usan `misma_sede: 0 / misma_ciudad: 30 /
otra_ciudad: 90` — valores de ejemplo, el operador los ajusta con
overrides. El engine de disponibilidad (fase futura) suma
`traslado + buffers + duración` al bloquear el recurso.

## 4. Términos de privacidad: versión, no booleano

`TenantPrivacyTerms` tiene `tenant_id` como PK (un texto vigente por
tenant). `contacts.privacy_terms_version` guarda la versión aceptada
(NULL = no aceptada). Si el tenant publica una versión nueva, la
comparación por versión obliga a re-aceptar — un booleano no podría
expresar eso. Si el perfil no trae `privacy_terms`, el onboarding no
crea la fila (decisión explícita, con test).

## 5. Citas: service_type_slug + venue

`Appointment.service_type_slug` (nullable, sin FK: las citas legacy y las
agendadas sin tipo siguen funcionando) y `venue` (sede del evento;
NULL en negocios fijos). `AppointmentResource` enlaza (cita, recurso)
con unique — el anti-traslape de recursos entre citas lo hará el engine
en una fase posterior usando estos enlaces.

## 6. Waitlist: máquina de estados mínima

`waiting → offered → converted | cancelled`. `current_appointment_id`
enlaza la oferta con la cita creada al convertir (nullable: aún no hay
cita mientras espera).

## 7. Compatibilidad

`schema_version: Literal["1.0", "1.1"]` con default "1.1". Los YAML 1.0
sin las secciones nuevas siguen validando (las tres colecciones son
opcionales con defaults vacíos/None). Los YAML 1.1 de esta fase son los
4 existentes actualizados + 2 nuevos (snacks_eventos, espejo_magico),
todos con contenido ficticio/genérico.

## Pendientes honestos (fuera de esta fase)

- El engine de agenda aún no usa `Resource`/`ServiceType`: `check_availability`
  y `book_appointment` no bloquean recursos ni calculan traslado. El schema
  es la base declarativa; la lógica de disponibilidad con recursos es la
  siguiente fase.
- `list_templates()` ahora reporta `recursos`, `tipos_servicio` y
  `privacy_terms`; el panel no los muestra todavía.
- La aceptación de términos desde el primer mensaje (guardar versión en
  el contacto) está modelada en BD pero no implementada en el webhook.
