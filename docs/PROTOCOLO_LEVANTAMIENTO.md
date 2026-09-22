# Protocolo de levantamiento por giro — Esqueleto Liah

> Relación con la alta: este protocolo es la **Fase A** de `GUIA_ALTA.md`
> (Día 0 — Perfil del negocio). La guía de alta describe el *cómo
> operativo*; este documento describe el *qué preguntar* y *dónde queda
> registrado* cada respuesta.

## 1. Objetivo y principio

El cliente omite información en la instalación y luego culpa al sistema.
Este protocolo existe para cerrar esa brecha de evidencia: el levantamiento
consiste en **las preguntas correctas por giro**, donde **cada respuesta
mapea a un campo de configuración concreto** del perfil del tenant. Nada de
lo acordado queda en una conversación verbal ni en un chat de WhatsApp:
todo lo relevante aterriza en el YAML del perfil o en los overrides de
alta.

La configuración instalada es **única y viva**: es una sola fuente de
verdad que usan tanto el chatbot (tono, respuestas, disponibilidad,
escalamiento) como el CRM (agenda, recordatorios, segmentación). El
levantamiento termina con el **ACTA DE CONFIGURACIÓN** firmada por el
cliente (sección 5): documento que certifica que lo instalado corresponde
exactamente a lo que el cliente declaró. Cambios posteriores requieren un
nuevo levantamiento y un acta nueva; la firma es la defensa contra el
reclamo de "eso no era lo que pedí".

**Principios:**

1. **Sin campo, no hay promesa.** Si la respuesta no cabe en un campo de
   configuración, no se promete el comportamiento; se documenta en el acta
   como limitación conocida o se cotiza como nuevo desarrollo.
2. **Una pregunta, un campo.** Cada pregunta del protocolo lleva su
   trazabilidad `→ campo`. Si el instalador improvisa preguntas fuera del
   protocolo, las mapea a mano a un campo antes de continuar.
3. **El cliente firma lo que declaró**, no lo que el instalador interpretó.
   El acta incluye las respuestas clave textuales.

---

## 2. Preguntas generales (todos los giros)

Se levantan para cualquier negocio antes de las preguntas del giro. El
prefijo de trazabilidad es `G-`.

### G1. Identidad y cuenta

| ID | Pregunta | → Campo |
|---|---|---|
| G-1 | ¿Cuál es el nombre comercial del negocio (como debe aparecer en mensajes y panel)? | → `nombre` (raíz del perfil, existente) |
| G-2 | ¿Qué slug quiere para su tenant (identificador único, minúsculas, sin espacios)? | → `slug` (raíz del perfil, existente) |
| G-3 | ¿En qué zona horaria opera (p. ej. `America/Mexico_City`)? | → `timezone` (raíz del perfil, existente) |
| G-4 | ¿Con qué tono debe hablar el asistente? (formal / cálido-profesional / amable-cercano / otro — pedir ejemplos de frases que sí y que no dirían) | → `tono` (raíz del perfil, existente) + `system_prompt` (raíz, existente) |
| G-5 | ¿Cuál es la dirección o ubicación del negocio (la que el bot dará a quien pregunte)? | → `conocimiento_semilla[]` (entrada "Ubicación"; existente) |

### G2. Horarios

| ID | Pregunta | → Campo |
|---|---|---|
| G-6 | Para cada día de la semana: ¿hora de apertura y cierre, o cerrado? | → `horarios` (existente) |
| G-7 | ¿Hay horarios especiales en días festivos o temporadas? ¿Cuáles? | → `conocimiento_semilla[]` (entrada "Horarios especiales"; existente) |

### G3. Recursos (qué se agenda: personas, espacios, equipos)

| ID | Pregunta | → Campo |
|---|---|---|
| G-8 | Liste **todo lo que se agenda o se asigna** a un servicio: personas (especialistas, staff), espacios (salas, consultorios) y equipos (máquinas, unidades móviles). Para cada uno: nombre. | → `resources[].nombre`, `resources[].slug` |
| G-9 | Para cada recurso: ¿es persona especialista, personal de apoyo, espacio físico o equipo? | → `resources[].tipo` (`room\|specialist\|equipment\|staff`) |
| G-10 | Para cada recurso: ¿es **fijo** (atiende en el local) o **móvil** (se desplaza al evento/domicilio del cliente)? | → `resources[].movilidad` (`fixed\|mobile`) |
| G-11 | Para cada recurso: ¿cuántas personas/turnos atiende **en paralelo**? (p. ej. una sala con 2 sillones = capacidad 2; default 1) | → `resources[].capacidad` |
| G-12 | Para cada especialista: ¿en qué especialidad o técnica? (informativo; sirve para filtrar por tipo de servicio) | → `resources[].especialidad` |

### G4. Tipos de servicio

| ID | Pregunta | → Campo |
|---|---|---|
| G-13 | Liste cada servicio que se vende o agenda: nombre y duración en minutos. | → `service_types[].nombre`, `service_types[].slug`, `service_types[].duracion_min` |
| G-14 | Para cada servicio: ¿qué recursos necesita y cuántos? (p. ej. "masaje relajante: 1 camilla + 1 masajista"; "chequeo: 1 consultorio + 1 médico de especialidad X") | → `service_types[].recursos[]` (`{recurso: <slug>}` o `{tipo: <tipo>, cantidad, especialidad?}`) |
| G-15 | Para cada servicio: ¿cuántos minutos de **preparación** (setup) antes y de **limpieza/desmontaje** (teardown) después requiere? | → `service_types[].buffers_min.setup`, `service_types[].buffers_min.teardown` |
| G-16 | Para cada servicio: ¿hay **traslado** involucrado? (no / fijo de X minutos / varía por zona) | → `service_types[].traslado.modo` (`none\|fixed\|per_zone`) |
| G-17 | Si el traslado es fijo: ¿cuántos minutos? Si es por zona: ¿qué zonas cubre y cuántos minutos por zona? | → `service_types[].traslado.fixed_min` / `service_types[].traslado.default_min` / `service_types[].traslado.zonas` (`{zona: minutos}`) |
| G-18 | ¿Cuáles son los precios de cada servicio (y si hay paquetes o promociones vigentes)? | → `conocimiento_semilla[]` (entrada "Servicios y precios"; existente) — el spec de configuración **no** incluye precio por servicio; el precio es conocimiento informativo, no lógica de agenda. |

### G5. Reglas de agenda (anticipación, cancelación, reprogramación)

| ID | Pregunta | → Campo |
|---|---|---|
| G-19 | ¿Con cuánta anticipación mínima se puede agendar? ¿Y cancelar o reprogramar sin costo? | → `reglas[]` (parámetros de la regla de agenda; existente) + `conocimiento_semilla[]` (entrada "Política de cancelación"; existente) |
| G-20 | ¿Qué pasa si el cliente no se presenta ni avisa? (p. ej. anticipo del 50% en la siguiente cita) | → `reglas[]` + `conocimiento_semilla[]` (existente) |
| G-21 | ¿Quiere recordatorios automáticos? ¿Cuántas horas antes? (24 h / 2 h / otro) | → `reglas[]` (regla `appointment_reminder`, params `hours_before`; existente) |

### G6. Aviso de privacidad y consentimientos

| ID | Pregunta | → Campo |
|---|---|---|
| G-22 | Entrégueme el **texto del aviso de privacidad** que el bot presentará en el primer contacto, su título y la fecha de la versión. | → `privacy_terms.titulo`, `privacy_terms.texto`, `privacy_terms.version` (fecha ISO) |
| G-23 | ¿Ya pide consentimiento a sus clientes para enviarles recordatorios por WhatsApp? ¿Cómo lo evidencia hoy (formato firmado, mensaje)? | → `reglas[]` (`appointment_reminder.require_consent`; existente). La evidencia histórica del cliente se archiva con el acta; no es campo del perfil. |

### G7. Campañas y opt-in

| ID | Pregunta | → Campo |
|---|---|---|
| G-24 | ¿Quiere enviar promociones o avisos (suspensión de clases, cambios de horario) por WhatsApp? | → decisión comercial (tier superior; ver `GUIA_ALTA.md` § Campañas). Se habilita en el alta; no es un campo del perfil de giro. |
| G-25 | ¿Cómo capturará el opt-in de sus contactos (palabra clave, toggle en panel, lista firmada importada)? | → operación en `/admin/campaigns` (`GUIA_ALTA.md`); la evidencia de importación masiva se archiva con el acta. |

### G8. Escalamiento a humano

| ID | Pregunta | → Campo |
|---|---|---|
| G-26 | ¿Ante qué temas el bot **siempre** debe pasar la conversación a una persona? (urgencias, quejas, temas legales, menores sin tutor…) | → `politicas.temas_sensibles[]` (existente) |
| G-27 | ¿Qué debe decir/hacer el bot al escalar? (mensaje al cliente, a quién avisa internamente y por qué medio) | → `politicas.escalamiento` (existente) |
| G-28 | Nombre y contacto (teléfono/WhatsApp) de la(s) persona(s) responsable(s) de atender los handoffs. | → se documenta en el acta; el medio de aviso interno se configura en el alta (no es campo del spec de giro). |

### G9. Conocimiento base

| ID | Pregunta | → Campo |
|---|---|---|
| G-29 | ¿Qué preguntas le hacen una y otra vez sus clientes? (top 10 con sus respuestas oficiales) | → `conocimiento_semilla[]` (entradas de FAQ; existente) |
| G-30 | ¿Qué **no** debe decir jamás el bot, aunque se lo pidan? (promesas de resultados, precios no autorizados, diagnósticos…) | → `politicas.temas_sensibles[]` y/o `system_prompt` (existentes) |
| G-31 | Documentos del negocio para ingerir (lista de servicios, políticas, menús, reglamento). | → ingesta a la base de conocimiento en Día 1–2 (`GUIA_ALTA.md`); el listado de documentos entregados queda en el acta. |

---

## 3. Preguntas específicas por giro

Cada bloque se aplica **después** del bloque general. El instalador elige
la plantilla del giro y levanta su bloque correspondiente.

> Nota: en `templates/` existen hoy `consultorio_medico`, `estetica`,
> `escuela_privada` y `academia_danza`. Los giros `snacks_eventos` y
> `espejo_magico` aún no tienen plantilla YAML; su bloque de preguntas
> define lo que la futura plantilla deberá cubrir.

### 3.1 Consultorio médico (`consultorio_medico`)

Prefijo `CM-`. Negocio fijo con agenda por especialista y consultorio; el
error caro es el traslape entre personas, especialistas y consultorios.

| ID | Pregunta | → Campo |
|---|---|---|
| CM-1 | ¿Qué **especialidades** atiende? (medicina general, pediatría, odontología, ginecología…) | → `resources[].especialidad` (en recursos de `tipo: specialist`) |
| CM-2 | ¿Cuántos **consultorios** (espacios físicos) tiene y cómo se llaman? | → `resources[]` (`tipo: room`, `movilidad: fixed`, `nombre`/`slug`) |
| CM-3 | ¿Qué especialista atiende en qué consultorio y en qué horario? (matriz especialista × consultorio × días) | → `service_types[].recursos[]` (combinación `{recurso: <slug-especialista>}` + `{recurso: <slug-consultorio>}` por servicio) |
| CM-4 | ¿Tienen citas **recurrentes**? (p. ej. pacientes con brackets que deben asistir una vez al mes) ¿Cada cuánto y para qué tratamientos? | → `reglas[]` (regla de recurrencia; existente) + `conocimiento_semilla[]` (existente) |
| CM-5 | ¿Atienden **urgencias** o solo cita programada? Si hay urgencias: ¿qué hace el bot cuando alguien escribe con una urgencia? | → `politicas.temas_sensibles[]` + `politicas.escalamiento` (existentes) |
| CM-6 | ¿Qué **datos de salud** pide o recibe el bot en la conversación? (síntomas, estudios, recetas) ¿Cuáles están prohibidos que el bot solicite o comente? | → `politicas.temas_sensibles[]` (existente). Nota: el spec **no** tiene un campo de "datos sensibles por servicio"; el manejo se expresa como temas que fuerzan escalamiento. |
| CM-7 | ¿Qué debe traer el paciente en su **primera visita**? (identificación, estudios previos, lista de medicamentos, acompañante si es menor) | → `conocimiento_semilla[]` (entrada "Primera visita"; existente) |
| CM-8 | ¿Atienden **menores de edad**? ¿Con qué condición? (acompañados de un adulto, autorización del tutor) | → `politicas.temas_sensibles[]` + `conocimiento_semilla[]` (existentes) |
| CM-9 | Para cada tipo de consulta: duración real (no la deseada), ¿requiere preparación del consultorio o equipo entre pacientes? | → `service_types[].duracion_min`, `service_types[].buffers_min` |

### 3.2 Estética / salón de belleza (`estetica`)

Prefijo `ES-`. Agenda por sillón/estación y estilista; los servicios se
encadenan (tinte + corte + peinado).

| ID | Pregunta | → Campo |
|---|---|---|
| ES-1 | ¿Cuántas **estaciones/sillones** tiene y qué servicio admite cada una? (corte, color, uñas, etc.) | → `resources[]` (`tipo: room` o `equipment`, `capacidad`) |
| ES-2 | ¿Cada estilista hace **todos** los servicios o hay especialidades? (colorista, barbero, manicurista…) | → `resources[].especialidad` (en `tipo: specialist`) |
| ES-3 | ¿Qué servicios se venden **encadenados** en una sola visita? (p. ej. tinte + corte) ¿Se agendan como un bloque o por separado? | → `service_types[]` (un `service_types` por bloque encadenado, con `duracion_min` total y `recursos[]` combinados) |
| ES-4 | ¿Algún servicio requiere **prueba previa** (alergia a tinte) o cita de valoración antes de agendar? | → `reglas[]` + `conocimiento_semilla[]` (existentes) |
| ES-5 | Tiempos muertos por servicio: ¿cuánto tarda el **procesamiento** (tinte en espera) durante el cual el estilista puede atender a otra clienta? | → `service_types[].buffers_min` (nota operativa en acta si el motor no lo modela aún) |

### 3.3 Escuela privada (`escuela_privada`)

Prefijo `EP-`. La "agenda" son visitas informativas y recorridos; el ciclo
comercial es la inscripción.

| ID | Pregunta | → Campo |
|---|---|---|
| EP-1 | ¿Qué **niveles** ofrece? (preescolar, primaria, secundaria, preparatoria) ¿La visita informativa es por nivel o general? | → `service_types[]` (un tipo por modalidad de visita) |
| EP-2 | ¿Quién da el **recorrido**? (director, coordinador de admisiones) ¿En qué espacio? (plantel, sala de admisiones) | → `resources[]` (`tipo: staff`/`room`, `movilidad: fixed`) |
| EP-3 | ¿Hay **temporada de inscripciones** con horarios o reglas distintas? (enero–marzo, etc.) | → `conocimiento_semilla[]` (entrada "Inscripciones"; existente) + `reglas[]` si hay ventanas (existente) |
| EP-4 | Temas sensibles propios: bullying, cuotas vencidas, quejas contra docentes, datos de menores. ¿Cuáles escalan siempre a humano? | → `politicas.temas_sensibles[]` (existente) |
| EP-5 | ¿Qué información **no** puede dar el bot sobre alumnos inscritos? (calificaciones, adeudos, datos personales de menores) | → `politicas.temas_sensibles[]` (existente) |
| EP-6 | Aviso de privacidad: ¿cubre **datos de menores**? ¿Quién otorga el consentimiento (madre/padre/tutor)? | → `privacy_terms.texto` (+ evidencia del aviso publicado, archivada con el acta) |

### 3.4 Academia de danza (`academia_danza`)

Prefijo `AD-`. Agenda por salón e instructor; la unidad comercial es la
clase de prueba y la mensualidad.

| ID | Pregunta | → Campo |
|---|---|---|
| AD-1 | ¿Cuántos **salones** tiene, qué capacidad (alumnos por clase) y qué disciplinas admite cada uno? | → `resources[]` (`tipo: room`, `capacidad`, `movilidad: fixed`) |
| AD-2 | ¿Qué **instructores** hay y qué disciplina/grupo de edad enseña cada uno? | → `resources[]` (`tipo: specialist`, `especialidad`) |
| AD-3 | ¿La **clase de prueba** es gratuita? ¿Cuántas puede tomar una persona y con qué anticipación se agenda? | → `service_types[]` (tipo "clase de prueba": `duracion_min`, `recursos[]`) + `reglas[]` (límite; existente) |
| AD-4 | ¿Hay **grupos por edad o nivel**? (infantil, juvenil, adultos; principiante/avanzado) ¿El bot debe preguntar la edad antes de agendar? | → `service_types[]` (tipos por grupo) + `reglas[]` (pregunta previa; existente) |
| AD-5 | Lesiones o menores sin tutor: ¿escalan a humano? ¿Qué mensaje da el bot? | → `politicas.temas_sensibles[]` + `politicas.escalamiento` (existentes) |
| AD-6 | Mensualidad y formas de pago: ¿el bot informa o solo agenda? (el cobro es fase 6 / desarrollo aparte) | → `conocimiento_semilla[]` (existente); el cobro **no** se promete en el alta estándar. |

### 3.5 Barras de snacks para eventos (`snacks_eventos`)

Prefijo `SE-`. **Negocio móvil**: no hay local; la disponibilidad depende
de fecha, horario, **traslado entre eventos** e instalación/desmontaje.

| ID | Pregunta | → Campo |
|---|---|---|
| SE-1 | ¿Qué **barras/productos** ofrecen? (esquites, hotcakes, nachos, paletas de hielo con toppings, botanas, sopas instantáneas…) ¿Cada barra es un servicio separado o se combinan en paquetes? | → `service_types[]` (un tipo por barra y por paquete) |
| SE-2 | ¿Qué **equipos y personal** requiere cada barra? (carrito, plancha, vitrina, 1–2 operadores) | → `resources[]` (`tipo: equipment`/`staff`, `movilidad: mobile`) y `service_types[].recursos[]` |
| SE-3 | ¿Cuánto tarda la **instalación** (setup) y el **desmontaje** (teardown) de cada barra en el lugar del evento? | → `service_types[].buffers_min.setup`, `service_types[].buffers_min.teardown` |
| SE-4 | ¿En qué **zonas/ciudades** dan servicio? Liste cada zona. | → `service_types[].traslado.zonas` (claves de zona) |
| SE-5 | **Traslado entre eventos**: ¿cuántos minutos agregan por zona para llegar e instalarse? ¿Hay un mínimo aunque el evento sea en su misma zona? | → `service_types[].traslado` (`modo: per_zone`, `zonas: {zona: minutos}`, `default_min`) |
| SE-6 | ¿Pueden atender **dos eventos el mismo día**? ¿Con qué separación mínima entre el fin de uno y el inicio del otro (servicio + desmontaje + traslado + instalación)? | → `service_types[].buffers_min` + `service_types[].traslado` (la regla de no-traslape vive en `reglas[]`; existente) |
| SE-7 | ¿El precio depende de **número de invitados**, horas de servicio o distancia? (rango de precios por barra) | → `conocimiento_semilla[]` (entrada "Precios y paquetes"; existente) — el spec **no** incluye precio ni cotizador por invitados. |
| SE-8 | ¿Piden **anticipo** para apartar fecha? ¿De cuánto y con cuánta anticipación se puede cancelar? | → `reglas[]` + `conocimiento_semilla[]` (existentes) |
| SE-9 | En el evento, ¿quién es el **contacto responsable** (nombre/teléfono) para coordinar la llegada del equipo? | → se documenta en el acta por evento; no es campo del perfil. |

### 3.6 Espejo mágico de fotos para eventos (`espejo_magico`)

Prefijo `EM-`. **Negocio móvil** de un solo equipo protagonista; el
cuello de botella es el espejo + su operador.

| ID | Pregunta | → Campo |
|---|---|---|
| EM-1 | ¿Cuántos **equipos de espejo mágico** tienen? ¿Cada uno opera con cuántos operadores? | → `resources[]` (`tipo: equipment`, `movilidad: mobile`, `capacidad`) + staff asociado en `service_types[].recursos[]` |
| EM-2 | ¿Qué **paquetes** venden? (horas de servicio, fotos impresas ilimitadas/limitadas, props, libro de firmas, personalización de marcos) | → `service_types[]` (un tipo por paquete: `duracion_min`, `recursos[]`) |
| EM-3 | **Instalación/desmontaje**: ¿cuánto tarda montar el espejo (nivelación, calibración, prueba de impresión) y desmontarlo? | → `service_types[].buffers_min.setup`, `service_types[].buffers_min.teardown` |
| EM-4 | ¿Qué **espacio y servicios** necesita el espejo en el venue? (m², contacto eléctrico, mesa, internet) | → `conocimiento_semilla[]` (entrada "Requisitos del lugar"; existente) |
| EM-5 | **Zonas de cobertura y traslados**: ¿a qué zonas viajan y cuántos minutos de traslado agregan por zona entre eventos del mismo día? | → `service_types[].traslado` (`modo: per_zone`, `zonas`, `default_min`) |
| EM-6 | ¿Pueden cubrir **dos eventos el mismo día** con el mismo equipo? ¿Separación mínima? | → `service_types[].buffers_min` + `traslado` + `reglas[]` (existente) |
| EM-7 | Personalización: ¿el cliente del evento elige **diseño de marco/plantilla**? ¿Con cuánta anticipación debe entregarlo? | → `reglas[]` (anticipación de entrega de diseño; existente) + `conocimiento_semilla[]` (existente) |
| EM-8 | Anticipo para apartar fecha y política de cancelación. | → `reglas[]` + `conocimiento_semilla[]` (existentes) |

---

## 4. Trazabilidad: resumen por campo

Matriz inversa: dado un campo del spec, qué preguntas lo alimentan. Sirve
para auditar que ningún campo quedó sin levantar.

| Campo del spec | Preguntas que lo alimentan |
|---|---|
| `resources[].slug` / `nombre` | G-8, CM-2, ES-1, EP-2, AD-1, AD-2, SE-2, EM-1 |
| `resources[].tipo` | G-9 |
| `resources[].movilidad` | G-10 (fijo por defecto; `mobile` en SE y EM) |
| `resources[].capacidad` | G-11, ES-1, AD-1, EM-1 |
| `resources[].especialidad` | G-12, CM-1, ES-2, AD-2 |
| `service_types[].slug` / `nombre` / `duracion_min` | G-13, CM-9, ES-3, EP-1, AD-3, AD-4, SE-1, EM-2 |
| `service_types[].recursos[]` | G-14, CM-3, SE-2, EM-1, EM-2 |
| `service_types[].buffers_min.setup` / `teardown` | G-15, CM-9, ES-5, SE-3, SE-6, EM-3, EM-6 |
| `service_types[].traslado.*` | G-16, G-17, SE-4, SE-5, SE-6, EM-5, EM-6 |
| `privacy_terms.{version,titulo,texto}` | G-22, EP-6 |
| `horarios` | G-6 |
| `politicas.temas_sensibles[]` | G-26, G-30, CM-5, CM-6, CM-8, EP-4, EP-5, AD-5 |
| `politicas.escalamiento` | G-27, CM-5, AD-5 |
| `reglas[]` | G-19, G-20, G-21, G-23, CM-4, CM-7*, ES-4, EP-3, AD-3, AD-4, SE-6, SE-8, EM-6, EM-7, EM-8 |
| `plantillas_hsm[]` | (se declaran en el alta a partir de `reglas[]`: recordatorios, confirmaciones, avisos — ver `GUIA_ALTA.md` Día 1–3) |
| `conocimiento_semilla[]` | G-5, G-7, G-18, G-19, G-20, G-29, G-31, CM-7, CM-8, ES-4, EP-3, AD-6, SE-7, SE-8, EM-4, EM-7, EM-8 |

\* CM-7 alimenta `conocimiento_semilla[]`; la cita recurrente CM-4 es la que
alimenta `reglas[]`.

### Preguntas sin campo directo en el spec (se documentan en el acta)

Estas preguntas son necesarias para el levantamiento pero **no** tienen un
campo en el spec canónico; su evidencia vive en el acta firmada o en la
operación del alta, no en el perfil:

- **G-1…G-4** (nombre, slug, zona horaria, tono): campos de la **raíz del
  perfil** ya existentes en las plantillas (`nombre`, `slug`, `timezone`,
  `tono`, `system_prompt`); no forman parte del spec de esta fase, pero ya
  existen en el esquema del perfil.
- **G-23** (evidencia histórica de consentimiento), **G-25** (evidencia de
  opt-in para importación masiva), **G-31** (lista de documentos
  entregados): evidencia archivada junto al acta.
- **G-24** (campañas): decisión comercial de tier; se opera en
  `/admin/campaigns`, no es campo del perfil de giro.
- **G-28** (responsable de handoffs): dato operativo del alta, archivado en
  el acta.
- **SE-9** (contacto responsable por evento): dato por evento, no del
  perfil; se captura en el CRM al agendar.
- **CM-6** (qué datos de salud puede pedir el bot): el spec no tiene campo
  de "datos sensibles por servicio"; se expresa como
  `politicas.temas_sensibles[]` (escalamiento) — el detalle fino queda en el
  acta.
- **ES-5** (tiempos de procesamiento en paralelo del estilista): el spec
  modela buffers secuenciales (`setup`/`teardown`); el paralelismo parcial
  se documenta en el acta como limitación conocida del motor.
- **G-18, SE-7** (precios): el spec de `service_types[]` **no** incluye
  precio; los precios viven en `conocimiento_semilla[]` como información,
  sin lógica de cotización automática.

---

## 5. ACTA DE CONFIGURACIÓN (formato listo para usar)

> Copiar, llenar y firmar al cierre del Día 0 (`GUIA_ALTA.md`). Un acta por
> tenant. Cambios posteriores a la firma requieren un **nuevo
> levantamiento** y un acta nueva con folio consecutivo.

---

# ACTA DE CONFIGURACIÓN — LIAH

**Folio:** ACTA-`<slug-tenant>`-`<AAAA-MM-DD>`-`<NN>`
**Fecha de levantamiento:** `<AAAA-MM-DD>`
**Cliente (razón social / nombre comercial):** `<nombre>`
**Giro:** `<consultorio_medico | estetica | escuela_privada | academia_danza | snacks_eventos | espejo_magico>`
**Plantilla base:** `templates/<giro>.yaml`
**Versión del perfil instalado:** `schema_version <v> + overrides <hash o fecha>`

## 1. Resumen de respuestas clave

*(Transcribir aquí las respuestas determinantes del levantamiento: recursos
dados de alta, tipos de servicio con duración y recursos asignados, buffers,
traslados por zona, horarios, anticipaciones de cancelación, temas que
escalan a humano, texto del aviso de privacidad y su versión. Cada línea
debe corresponder a un campo del perfil.)*

- …
- …

## 2. Declaración de conformidad

El cliente declara que:

1. Las respuestas de la sección 1 fueron **proporcionadas por él** (o por
   personal autorizado) durante el levantamiento del `<fecha>`.
2. La configuración instalada en su tenant corresponde **exactamente** a lo
   declarado en la sección 1. Verificó el resumen antes de firmar.
3. Entiende que el asistente y el CRM operan **únicamente** con esta
   configuración: lo no declarado aquí no existe para el sistema.
4. Entiende que **cualquier cambio** (nuevos servicios, horarios, precios,
   políticas, zonas, personal) requiere un **nuevo levantamiento** y la
   firma de una nueva acta; los cambios no aplican retroactivamente a citas
   o campañas ya programadas.
5. Recibió copia de esta acta y del resumen de su configuración.

## 3. Limitaciones conocidas aceptadas

*(P. ej.: "los precios son informativos, el bot no cotiza automáticamente";
"el procesamiento de tinte en espera no libera al estilista en el motor
actual"; "sin plantillas HSM aprobadas por Meta no hay mensajes
proactivos".)*

- …

## 4. Firmas

| | Cliente | Instalador (operador Liah) |
|---|---|---|
| Nombre | | |
| Cargo | | |
| Firma | | |
| Fecha | | |

**Cláusula de cambios posteriores:** todo ajuste a la configuración después
de esta firma se levanta con el mismo protocolo, genera un acta nueva con
folio consecutivo y deja sin efecto la presente en lo modificado. Ningún
acuerdo verbal, mensaje de chat o correo sustituye al acta firmada.

---

## 6. Regla de cierre del levantamiento (checklist del instalador)

Antes de pedir la firma, el instalador verifica:

- [ ] Cada respuesta del cliente tiene su `→ campo` anotado (secciones 2–3).
- [ ] Ningún campo del spec quedó vacío sin justificación en el acta
      (usar la matriz de la sección 4 como auditoría inversa).
- [ ] Las preguntas "sin campo directo" (sección 4) quedaron documentadas
      en el acta o archivadas como evidencia.
- [ ] El cliente leyó el resumen de la sección 1 del acta y confirma que
      es lo que declaró.
- [ ] El aviso de privacidad (`privacy_terms`) está publicado por el
      cliente y el texto firmado coincide con el instalado.
- [ ] Firmas y folio consecutivo registrados; copia entregada al cliente.
