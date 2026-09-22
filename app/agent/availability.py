"""Motor de disponibilidad por recursos (Fase 7c).

Responde: ¿se puede agendar `service_type_slug` en (fecha, hora, venue)?
Cero literales de vertical: todo lo variable (recursos, duraciones,
buffers, traslados) viene de `resources`/`service_types` del tenant.

Modelo:
- Cada `ServiceType` declara `recursos_requeridos`: lista de
  {"recurso": slug} (recurso exacto) o {"tipo": t, "cantidad": n,
  "especialidad": e} (los primeros n por slug del tipo t, filtrando
  especialidad si se indica).
- Intervalo ocupado de una cita = [inicio − setup, inicio + duración +
  teardown] (buffers del propio tipo de servicio de cada cita).
- Recurso: si los traslapes confirmados sobre su intervalo ocupado son
  >= capacidad → conflicto ("no traslapar personas ni lugares").
- Traslado (solo recursos con movilidad "mobile"): entre reservas del
  mismo día del recurso debe caber el tiempo de viaje entre venues.
- Persona: las propias citas confirmadas del contacto también bloquean.

Convención de tiempo: datetimes naive = hora local del tenant (igual que
`app/agent/calendar.py`).
"""
import logging
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentResource,
    Resource,
    ServiceType,
)

logger = logging.getLogger("liah.availability")

# Ventana para buscar alternativas cercanas (misma convención que
# calendar.py: hora local del tenant).
ALT_DAY_START_HOUR = 8
ALT_DAY_END_HOUR = 20
ALT_STEP_MINUTES = 30
ALT_MAX_RESULTS = 3
ALT_LOOKAHEAD_DAYS = 2


# ── parsing / normalización ────────────────────────────────────────────

def _parse_start(date: str, start_hhmm: str) -> datetime:
    """Parsea 'YYYY-MM-DD' + 'HH:MM[-HH:MM]' en hora local (naive)."""
    start_key = (start_hhmm or "").split("-")[0].strip()
    return datetime.strptime(f"{date} {start_key}", "%Y-%m-%d %H:%M")


def _norm(text: str | None) -> str:
    """Normaliza para comparar venues/zonas: minúsculas, sin acentos."""
    text = (text or "").strip().lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(text.split())


def _buffers(st: ServiceType) -> tuple[int, int]:
    b = st.buffers or {}
    return int(b.get("setup") or 0), int(b.get("teardown") or 0)


def _occupied_interval(start_at: datetime, st: ServiceType) -> tuple[datetime, datetime]:
    """[inicio − setup, inicio + duración + teardown]."""
    setup, teardown = _buffers(st)
    return (
        start_at - timedelta(minutes=setup),
        start_at + timedelta(minutes=st.duracion_min + teardown),
    )


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Traslape estricto: tocarse en el borde NO es traslape."""
    return a_start < b_end and b_start < a_end


# ── carga de configuración ─────────────────────────────────────────────

async def _load_service_type(
    session: AsyncSession, tenant_id, slug: str
) -> ServiceType | None:
    return (
        await session.execute(
            select(ServiceType).where(
                ServiceType.tenant_id == tenant_id,
                ServiceType.slug == slug,
            )
        )
    ).scalar_one_or_none()


async def _resolve_resources(
    session: AsyncSession, tenant_id, st: ServiceType
) -> tuple[list[Resource], str | None]:
    """Resuelve `recursos_requeridos` a recursos concretos.

    Devuelve (recursos, error). `error` no-None = no se pudo resolver.
    """
    resolved: list[Resource] = []
    for spec in st.recursos_requeridos or []:
        if not isinstance(spec, dict):
            return [], f"especificación de recurso inválida en '{st.slug}'"
        if spec.get("recurso"):
            r = (
                await session.execute(
                    select(Resource).where(
                        Resource.tenant_id == tenant_id,
                        Resource.slug == spec["recurso"],
                    )
                )
            ).scalar_one_or_none()
            if r is None:
                return [], (
                    f"el tipo de servicio '{st.slug}' requiere el recurso "
                    f"'{spec['recurso']}', que no existe"
                )
            resolved.append(r)
        elif spec.get("tipo"):
            tipo = spec["tipo"]
            cantidad = int(spec.get("cantidad") or 1)
            especialidad = spec.get("especialidad")
            q = select(Resource).where(
                Resource.tenant_id == tenant_id,
                Resource.tipo == tipo,
            )
            if especialidad:
                q = q.where(Resource.especialidad == especialidad)
            cands = (await session.execute(q.order_by(Resource.slug))).scalars().all()
            if len(cands) < cantidad:
                detalle = f"tipo '{tipo}'"
                if especialidad:
                    detalle += f" especialidad '{especialidad}'"
                return [], (
                    f"no hay suficientes recursos ({detalle}): se necesitan "
                    f"{cantidad}, hay {len(cands)}"
                )
            resolved.extend(cands[:cantidad])
        else:
            return [], (
                f"especificación de recurso inválida en '{st.slug}': "
                "usa {'recurso': slug} o {'tipo': t, 'cantidad': n}"
            )
    # Sin duplicados (un recurso puede aparecer en dos specs).
    seen, uniq = set(), []
    for r in resolved:
        if r.id not in seen:
            seen.add(r.id)
            uniq.append(r)
    return uniq, None


# ── reservas del día ───────────────────────────────────────────────────

async def _day_bookings_for_resource(
    session: AsyncSession, tenant_id, resource: Resource, day: datetime.date
) -> list[tuple["Appointment", ServiceType | None]]:
    """Citas confirmadas del día enlazadas al recurso (con su ServiceType)."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    rows = (
        await session.execute(
            select(Appointment)
            .join(
                AppointmentResource,
                AppointmentResource.appointment_id == Appointment.id,
            )
            .where(
                Appointment.tenant_id == tenant_id,
                AppointmentResource.resource_id == resource.id,
                AppointmentResource.tenant_id == tenant_id,
                Appointment.status == "confirmed",
                Appointment.start_at >= day_start,
                Appointment.start_at < day_end,
            )
            .order_by(Appointment.start_at.asc())
        )
    ).scalars().all()
    out = []
    st_cache: dict[str | None, ServiceType | None] = {}
    for appt in rows:
        slug = appt.service_type_slug
        if slug not in st_cache:
            st_cache[slug] = (
                await _load_service_type(session, tenant_id, slug)
                if slug else None
            )
        out.append((appt, st_cache[slug]))
    return out


async def _day_bookings_for_contact(
    session: AsyncSession, tenant_id, contact_id, day: datetime.date
) -> list[tuple["Appointment", ServiceType | None]]:
    """Citas confirmadas del día del contacto (cualquier recurso)."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    rows = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == tenant_id,
                Appointment.contact_id == contact_id,
                Appointment.status == "confirmed",
                Appointment.start_at >= day_start,
                Appointment.start_at < day_end,
            )
        )
    ).scalars().all()
    out = []
    st_cache: dict[str | None, ServiceType | None] = {}
    for appt in rows:
        slug = appt.service_type_slug
        if slug not in st_cache:
            st_cache[slug] = (
                await _load_service_type(session, tenant_id, slug)
                if slug else None
            )
        out.append((appt, st_cache[slug]))
    return out


# ── traslado ───────────────────────────────────────────────────────────

def _travel_minutes(traslado_cfg: dict, venue_from: str | None,
                    venue_to: str | None) -> int:
    """Minutos de viaje entre dos venues según la política del ServiceType.

    Matcheo de zona (diseño simple y testeado):
    - Si origen y destino normalizan igual (incluye ambos vacíos) → 0.
    - modo "none" → 0 (negocio fijo o sin costo de tiempo).
    - modo "fixed" → `fixed_min`.
    - modo "per_zone" → igualdad exacta (normalizada) del venue DESTINO
      contra las claves de `zonas`; sin coincidencia → `default_min`.
      El operador nombra las zonas con los labels que usa en `venue`
      (p.ej. {"xalapa": 0, "veracruz": 45}); las etiquetas genéricas de
      las plantillas (misma_sede/misma_ciudad/otra_ciudad) son ejemplos.
    """
    cfg = traslado_cfg or {}
    a, b = _norm(venue_from), _norm(venue_to)
    if a == b:
        return 0
    modo = (cfg.get("modo") or "none").lower()
    if modo == "none":
        return 0
    if modo == "fixed":
        return int(cfg.get("fixed_min") or 0)
    if modo == "per_zone":
        zonas = cfg.get("zonas") or {}
        norm_zonas = {_norm(k): v for k, v in zonas.items()}
        if b in norm_zonas:
            return int(norm_zonas[b] or 0)
        return int(cfg.get("default_min") or 0)
    return 0


def _check_travel(
    st: ServiceType,
    resource: Resource,
    bookings: list[tuple["Appointment", ServiceType | None]],
    cand_start: datetime,
    cand_end: datetime,
    venue: str | None,
    exclude_id,
) -> str | None:
    """Verifica que quepa el traslado con los vecinos del día.

    Devuelve None si hay hueco suficiente, o el motivo del conflicto.
    """
    if resource.movilidad != "mobile":
        return None
    # Vecinos: última reserva que termina antes del candidato y primera
    # que empieza después (intervalos ocupados, con buffers propios).
    prev = None   # (occupied_end, venue)
    nxt = None    # (occupied_start, venue)
    for appt, appt_st in bookings:
        if exclude_id is not None and appt.id == exclude_id:
            continue
        ist = appt_st or st  # legacy sin tipo: usa el del candidato
        b_start, b_end = _occupied_interval(appt.start_at, ist)
        if b_end <= cand_start:
            if prev is None or b_end > prev[0]:
                prev = (b_end, appt.venue)
        elif b_start >= cand_end:
            if nxt is None or b_start < nxt[0]:
                nxt = (b_start, appt.venue)
        # Si traslapa al candidato, el chequeo de capacidad ya lo marcó
        # (o la capacidad > 1 lo permite: el traslado contra un traslape
        #  parcial es ambiguo y se omite por diseño).
    if prev is not None:
        gap_min = (cand_start - prev[0]).total_seconds() / 60
        need = _travel_minutes(st.traslado, prev[1], venue)
        if gap_min < need:
            return (
                f"traslado insuficiente para '{resource.slug}': se necesitan "
                f"{need} min entre '{prev[1] or 'sede'}' y "
                f"'{venue or 'sede'}', hay {int(gap_min)} min"
            )
    if nxt is not None:
        gap_min = (nxt[0] - cand_end).total_seconds() / 60
        need = _travel_minutes(st.traslado, venue, nxt[1])
        if gap_min < need:
            return (
                f"traslado insuficiente para '{resource.slug}': se necesitan "
                f"{need} min entre '{venue or 'sede'}' y "
                f"'{nxt[1] or 'sede'}', hay {int(gap_min)} min"
            )
    return None


# ── chequeo principal ──────────────────────────────────────────────────

def _empty_result() -> dict:
    return {"available": False, "reason": None, "conflicts": [],
            "alternatives": []}


async def _check_core(
    session: AsyncSession,
    tenant_id,
    st: ServiceType,
    resolved: list[Resource],
    day: datetime.date,
    start_at: datetime,
    venue: str | None,
    exclude_appointment_id,
    contact_id,
) -> dict:
    """Chequeo sin alternativas (las probes las desactivan para no recursar)."""
    result = _empty_result()
    cand_start, cand_end = _occupied_interval(start_at, st)

    # 1) Capacidad por recurso: traslapes confirmados sobre el intervalo.
    for resource in resolved:
        bookings = await _day_bookings_for_resource(
            session, tenant_id, resource, day
        )
        overlapping = 0
        for appt, appt_st in bookings:
            if exclude_appointment_id is not None and appt.id == exclude_appointment_id:
                continue
            ist = appt_st or st
            b_start, b_end = _occupied_interval(appt.start_at, ist)
            if _overlaps(cand_start, cand_end, b_start, b_end):
                overlapping += 1
        if overlapping >= resource.capacidad:
            result["conflicts"].append({
                "type": "resource_capacity",
                "resource": resource.slug,
                "detail": (
                    f"recurso '{resource.slug}' ocupado en ese horario "
                    f"(capacidad {resource.capacidad})"
                ),
            })

        # 2) Traslado (solo mobile).
        travel_err = _check_travel(
            st, resource, bookings, cand_start, cand_end, venue,
            exclude_appointment_id,
        )
        if travel_err:
            result["conflicts"].append({
                "type": "travel",
                "resource": resource.slug,
                "detail": travel_err,
            })

    # 3) Traslape de la propia persona (sus citas, cualquier recurso).
    if contact_id is not None:
        own = await _day_bookings_for_contact(
            session, tenant_id, contact_id, day
        )
        for appt, appt_st in own:
            if exclude_appointment_id is not None and appt.id == exclude_appointment_id:
                continue
            ist = appt_st or st
            b_start, b_end = _occupied_interval(appt.start_at, ist)
            if _overlaps(cand_start, cand_end, b_start, b_end):
                result["conflicts"].append({
                    "type": "person_overlap",
                    "resource": None,
                    "detail": "ya tienes una cita en ese horario",
                })
                break

    if result["conflicts"]:
        result["reason"] = result["conflicts"][0]["detail"]
    else:
        result["available"] = True
    return result


async def _nearby_alternatives(
    session: AsyncSession,
    tenant_id,
    st: ServiceType,
    resolved: list[Resource],
    day: datetime.date,
    start_at: datetime,
    venue: str | None,
    exclude_appointment_id,
    contact_id,
) -> list[str]:
    """Slots cercanos reales: mismo día (ventana 8–20, pasos de 30 min) y
    misma hora en los días siguientes. Llama al chequeo sin recursar."""
    requested = (day.isoformat(), start_at.strftime("%H:%M"))
    probes: list[tuple[datetime.date, str]] = []
    t = datetime.combine(day, datetime.min.time()).replace(
        hour=ALT_DAY_START_HOUR)
    end = t.replace(hour=ALT_DAY_END_HOUR)
    while t <= end:
        key = (day.isoformat(), t.strftime("%H:%M"))
        if key != requested:
            probes.append((day, t.strftime("%H:%M")))
        t += timedelta(minutes=ALT_STEP_MINUTES)
    for d in range(1, ALT_LOOKAHEAD_DAYS + 1):
        probes.append((day + timedelta(days=d), start_at.strftime("%H:%M")))

    out = []
    for pday, phhmm in probes:
        try:
            pstart = datetime.strptime(
                f"{pday.isoformat()} {phhmm}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        core = await _check_core(
            session, tenant_id, st, resolved, pday, pstart, venue,
            exclude_appointment_id, contact_id,
        )
        if core["available"]:
            out.append(f"{pday.isoformat()} {phhmm}")
            if len(out) >= ALT_MAX_RESULTS:
                break
    return out


# ── API pública ────────────────────────────────────────────────────────

async def check_resource_availability(
    session: AsyncSession,
    tenant_id,
    service_type_slug: str,
    date: str,
    start_hhmm: str,
    venue: str | None = None,
    exclude_appointment_id=None,
    contact_id=None,
) -> dict:
    """¿Hay disponibilidad para `service_type_slug` en fecha/hora/venue?

    `exclude_appointment_id`: cita a ignorar (re-agendar / hueco liberado).
    `contact_id`: si se da, también se bloquea el traslape con las propias
    citas del contacto.

    Devuelve {"available": bool, "reason": str|None,
              "conflicts": [...], "alternatives": ["YYYY-MM-DD HH:MM", ...]}.
    """
    result = _empty_result()

    st = await _load_service_type(session, tenant_id, service_type_slug or "")
    if st is None:
        result["reason"] = (
            f"tipo de servicio desconocido: '{service_type_slug}'"
        )
        result["conflicts"].append({
            "type": "config", "resource": None, "detail": result["reason"],
        })
        return result

    try:
        start_at = _parse_start(date, start_hhmm)
    except ValueError:
        result["reason"] = "formato inválido: usa YYYY-MM-DD y HH:MM"
        result["conflicts"].append({
            "type": "config", "resource": None, "detail": result["reason"],
        })
        return result

    resolved, resolve_err = await _resolve_resources(session, tenant_id, st)
    if resolve_err:
        result["reason"] = resolve_err
        result["conflicts"].append({
            "type": "config", "resource": None, "detail": resolve_err,
        })
        return result

    if exclude_appointment_id is not None and not hasattr(
        exclude_appointment_id, "hex"
    ):
        try:
            import uuid as _uuid
            exclude_appointment_id = _uuid.UUID(str(exclude_appointment_id))
        except ValueError:
            pass

    day = start_at.date()
    core = await _check_core(
        session, tenant_id, st, resolved, day, start_at, venue,
        exclude_appointment_id, contact_id,
    )
    if not core["available"]:
        # Solo los conflictos reales (no errores de config) generan
        # alternativas cercanas.
        core["alternatives"] = await _nearby_alternatives(
            session, tenant_id, st, resolved, day, start_at, venue,
            exclude_appointment_id, contact_id,
        )
    return core


async def resolve_service_resources(
    session: AsyncSession, tenant_id, service_type_slug: str
) -> tuple[ServiceType | None, list[Resource], str | None]:
    """Carga el ServiceType y resuelve sus recursos (para calendar.book).

    Devuelve (service_type, recursos, error). `error` no-None = no usar.
    """
    st = await _load_service_type(session, tenant_id, service_type_slug or "")
    if st is None:
        return None, [], f"tipo de servicio desconocido: '{service_type_slug}'"
    resolved, err = await _resolve_resources(session, tenant_id, st)
    if err:
        return st, [], err
    return st, resolved, None
