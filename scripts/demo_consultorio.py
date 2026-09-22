#!/usr/bin/env python3
"""Demo end-to-end del esqueleto Liah — vertical CONSULTORIO MÉDICO (Fase 5).

Da de alta una clínica ficticia y genérica con la plantilla
`templates/consultorio_medico.yaml` vía el onboarding REAL
(`app/api/onboarding.py::onboard_tenant`, la misma función que usan el CLI y
el endpoint), y prueba las 4 rutas del producto validando RESULTADOS reales
(estado en BD / texto final), no solo retrieval:

  (1) Conocimiento:  "¿cuánto cuesta la consulta general?"
      -> la RESPUESTA FINAL contiene el precio del seed ($600).
  (2) Acción:        agendar por `run_agent` (ruta completa del engine, con
      un StubLLM determinista que emite tool-calling realista); verifica la
      cita en BD; reintenta -> UNA sola cita; luego cancela y reprograma por
      la misma ruta.
  (3) Handoff:       "me duele mucho el pecho, ¿qué hago?" -> se CREA el
      Handoff, `conversation.mode == "human"`, y el bot deja de responder
      (probado con el drenador real de `webhook_jobs`).
  (4) Recordatorios: cita próxima + regla `appointment_reminder` ->
      el scheduler genera `reminder_log` sin duplicar en segunda corrida.

BD: usa `pyme_agent_demo` (se crea si no existe), NUNCA la BD de test de
pytest. Cualquier `drop_all`/reset exige `--reset` o `LIAH_DEMO_RESET=1`:
por default no borra nada (reusa el tenant si el slug ya existe y limpia
solo sus propios artefactos del run anterior).

Uso:
    python scripts/demo_consultorio.py            # corrida normal
    python scripts/demo_consultorio.py --reset    # borra y empieza de cero

Veredicto: imprime `DEMO OK (4/4 rutas)` o la lista de fallos con causa;
el exit code es != 0 si algo falla.
"""
import argparse
import asyncio
import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# La demo fija su BD ANTES de importar la app (mismo patrón que
# tests/conftest.py): nunca toca la BD de test de pytest.
DEMO_DB_URL = os.getenv(
    "LIAH_DEMO_DATABASE_URL",
    "postgresql+asyncpg://pyme:pyme@127.0.0.1:5433/pyme_agent_demo",
)
os.environ["DATABASE_URL"] = DEMO_DB_URL
os.environ.setdefault("LIAH_SEND_DRY_RUN", "1")  # jamás llamar a Meta en la demo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.agent.calendar import MemoryCalendarAdapter  # noqa: E402
from app.agent.embedder import FakeEmbedder  # noqa: E402
from app.agent.engine import HANDOFF_NOTICE, run_agent  # noqa: E402
from app.agent.ports import LLMResponse  # noqa: E402
from app.agent.rag import ingest_knowledge  # noqa: E402
from app.api.onboarding import onboard_tenant  # noqa: E402
from app.channels.whatsapp.queue import drain_jobs, enqueue_job  # noqa: E402
from app.core import db as db_mod  # noqa: E402
from app.core.base import Base  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    ActionLog,
    Appointment,
    Contact,
    Conversation,
    Handoff,
    KnowledgeSource,
    Message,
    ReminderLog,
    Tenant,
    TenantPrivacyTerms,
)
from app.models.conversations import MODE_HUMAN, get_or_create_conversation  # noqa: E402
from app.reminders import scheduler as scheduler_mod  # noqa: E402

TEMPLATE = "consultorio_medico"
SLUG = "clinica-demo"
TENANT_TZ = "America/Mexico_City"
EXPECTED_PRICE = "$600"

QUESTION = "¿cuánto cuesta la consulta general?"
# Chunk FAQ de apoyo: FakeEmbedder es léxico (no semántico como OpenAI), así
# que una pregunta natural no alcanza el umbral 0.75 contra los chunks largos
# del seed. Este FAQ vive en el KB real del tenant, se ingiere por el pipeline
# real y se recupera por el guard RAG real del engine (similitud ~0.85).
# Con embeddings de OpenAI el seed solo bastaría.
FAQ_TITLE = "FAQ: precio de consulta general"
FAQ_CONTENT = "¿Cuánto cuesta la consulta general? Cuesta $600 MXN."

URGENCY_TEXT = "me duele mucho el pecho, ¿qué hago?"


# ── Stubs LLM deterministas (imitan tool-calling realista) ──────
def _tc(id_, name, arguments):
    return {"id": id_, "name": name, "arguments": arguments}


class _ScriptedLLM:
    """Devuelve una secuencia programada de respuestas (tool-calling real)."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, messages, tools=None, tool_choice=None):
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="fin", finish_reason="stop")


class _KnowledgeEchoLLM:
    """Eco del tool result: lo que el RAG recuperó es lo que responde.

    No alucina el precio: si el RAG no trae nada, la respuesta final no
    contiene el precio y el veredicto falla (eso es lo que se quiere probar).
    """

    def __init__(self, question: str):
        self._question = question

    async def chat(self, messages, tools=None, tool_choice=None):
        for m in reversed(messages):
            if m.get("role") == "tool":
                data = json.loads(m["content"])
                results = data.get("results") or []
                if results:
                    return LLMResponse(
                        content="Con gusto. " + results[0]["content"],
                        finish_reason="stop",
                    )
                return LLMResponse(
                    content="No encontré esa información en este momento.",
                    finish_reason="stop",
                )
        return LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[_tc("k1", "search_knowledge_base",
                            {"query": self._question})],
        )


class _UrgencyLLM:
    """Ante el mensaje de urgencia emite escalate_to_human (como haría un LLM
    real con el system prompt de la plantilla: 'escalas INMEDIATAMENTE')."""

    async def chat(self, messages, tools=None, tool_choice=None):
        user_text = next(
            (m["content"] for m in reversed(messages)
             if m.get("role") == "user"),
            "",
        )
        if "pecho" in user_text.lower():
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[_tc("e1", "escalate_to_human",
                                {"reason": "posible urgencia médica: dolor de pecho"})],
            )
        return LLMResponse(content="Entendido, ¿en qué más le ayudo?",
                           finish_reason="stop")


# ── Infra de la demo ───────────────────────────────────────────
def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Demo end-to-end: consultorio médico.")
    p.add_argument("--reset", action="store_true",
                   help="Borra el esquema de la BD demo y empieza de cero.")
    return p.parse_args(argv)


async def _ensure_database(db_url: str) -> None:
    """Crea la BD demo si no existe (nunca toca otras BDs)."""
    parsed = urlparse(db_url.replace("+asyncpg", ""))
    dbname = parsed.path.lstrip("/")
    admin_url = f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/postgres"
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
            print(f"  [db] creada base de datos '{dbname}'")
        else:
            print(f"  [db] base de datos '{dbname}' ya existe (se reusa)")
    finally:
        await conn.close()


def _wire_db(db_url: str) -> None:
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


async def _prepare_schema(reset: bool) -> None:
    url = DEMO_DB_URL.replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    finally:
        await conn.close()
    async with db_mod.engine.begin() as conn:
        if reset:
            print("  [db] --reset: drop_all + create_all")
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _get_or_create_tenant(session) -> uuid.UUID:
    existing = (
        await session.execute(select(Tenant).where(Tenant.slug == SLUG))
    ).scalar_one_or_none()
    if existing is not None:
        print(f"  [onboard] tenant '{SLUG}' ya existe: se reusa "
              f"(sin --reset no se recrea)")
        return existing.id
    print(f"  [onboard] dando de alta '{SLUG}' con plantilla '{TEMPLATE}' "
          f"(onboarding real, transaccional)...")
    result = await onboard_tenant(
        session,
        template_name=TEMPLATE,
        slug=SLUG,
        nombre="Clínica Demo Liah",
        overrides=None,
        admin_email="demo@clinica-demo.example",
        admin_password=secrets.token_urlsafe(16),  # solo para el alta; no se muestra
        embedder=FakeEmbedder(),
    )
    print(f"  [onboard] ok: tenant_id={result['tenant_id']} "
          f"giro={result['giro']} resumen={result['summary']}")
    return uuid.UUID(result["tenant_id"])


async def _ensure_faq_chunk(session, tenant_id) -> None:
    """El FAQ de apoyo existe una sola vez (idempotente entre corridas)."""
    exists = (
        await session.execute(
            select(KnowledgeSource.id).where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.title == FAQ_TITLE,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        await ingest_knowledge(
            session, tenant_id, FAQ_TITLE, FAQ_CONTENT, FakeEmbedder()
        )
        print("  [kb] FAQ de apoyo ingestado (ver docstring del script)")
    else:
        print("  [kb] FAQ de apoyo ya existe")


async def _get_or_create_contact(session, tenant_id, wa_id,
                                 name=None, consent="granted") -> Contact:
    # Fase 7c: la puerta de privacidad exige granted + VERSIÓN vigente de
    # los términos. Un contacto "granted" sin versión queda bloqueado al
    # agendar, así que al otorgar se fija la versión actual del tenant.
    terms_version = None
    if consent == "granted":
        terms_version = (
            await session.execute(
                select(TenantPrivacyTerms.version).where(
                    TenantPrivacyTerms.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
    contact = (
        await session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id, Contact.wa_id == wa_id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(tenant_id=tenant_id, wa_id=wa_id, name=name,
                          consent_status=consent,
                          privacy_terms_version=terms_version)
        session.add(contact)
        await session.flush()
    elif contact.consent_status != consent:
        contact.consent_status = consent
        contact.privacy_terms_version = terms_version
        await session.flush()
    elif (consent == "granted"
          and contact.privacy_terms_version != terms_version):
        # Contacto legacy (alta anterior a la puerta de privacidad):
        # se auto-repara la versión sin cambiar nada más.
        contact.privacy_terms_version = terms_version
        await session.flush()
    return contact


async def _cleanup_route2(session, tenant_id, contact_id) -> None:
    """La demo limpia SUS propios artefactos del run anterior (citas del
    contacto demo + sus idempotency keys de booking)."""
    await session.execute(
        delete(Appointment).where(
            Appointment.tenant_id == tenant_id,
            Appointment.contact_id == contact_id,
        )
    )
    await session.execute(
        delete(ActionLog).where(
            ActionLog.tenant_id == tenant_id,
            ActionLog.contact_id == contact_id,
            ActionLog.action == "book_appointment",
        )
    )
    await session.commit()


def _report(results: list[tuple[str, bool, str]]) -> int:
    print()
    print("=" * 64)
    ok = sum(1 for _, passed, _ in results if passed)
    for name, passed, detail in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
    print("=" * 64)
    if ok == len(results):
        print(f"DEMO OK ({ok}/{len(results)} rutas)")
        return 0
    print(f"DEMO CON FALLOS ({ok}/{len(results)} rutas)")
    return 1


# ── Rutas ──────────────────────────────────────────────────────
async def _route_knowledge(session, tenant_id, contact) -> tuple[bool, str]:
    reply = await run_agent(
        session, _KnowledgeEchoLLM(QUESTION), tenant_id, contact.id, QUESTION,
        embedder=FakeEmbedder(),
    )
    if EXPECTED_PRICE in reply:
        return True, f"la respuesta final contiene '{EXPECTED_PRICE}'"
    return False, (f"la respuesta final NO contiene '{EXPECTED_PRICE}'. "
                   f"Respuesta: {reply[:200]!r}")


async def _route_booking(session, tenant_id, contact) -> tuple[bool, str]:
    sm = db_mod.async_session_maker
    target = (datetime.now(ZoneInfo(TENANT_TZ)) + timedelta(days=1)).date().isoformat()

    def _book_script():
        return [
            LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                _tc("c1", "check_availability",
                    {"date": target, "time_slot": "10:00"})]),
            LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                _tc("c2", "book_appointment",
                    {"date": target, "time_slot": "10:00",
                     "type": "consultation"})]),
            LLMResponse(content="Listo, tu cita quedó agendada.",
                        finish_reason="stop"),
        ]

    async with sm() as s:
        reply = await run_agent(
            s, _ScriptedLLM(_book_script()), tenant_id, contact.id,
            "Quiero agendar una cita para mañana a las 10",
            embedder=FakeEmbedder(),
        )
        appt = (
            await s.execute(
                select(Appointment).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact.id,
                    Appointment.status == "confirmed",
                )
            )
        ).scalar_one_or_none()
    if appt is None:
        return False, f"no se creó la cita en BD. Respuesta: {reply[:160]!r}"

    # Reintento del mismo intento -> UNA sola cita (guard anti-doble-agenda +
    # idempotency key determinista del engine).
    async with sm() as s:
        await run_agent(
            s, _ScriptedLLM(_book_script()), tenant_id, contact.id,
            "Quiero agendar una cita para mañana a las 10",
            embedder=FakeEmbedder(),
        )
        n = (
            await s.execute(
                select(func.count()).select_from(Appointment).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact.id,
                )
            )
        ).scalar()
    if n != 1:
        return False, f"el reintento duplicó la cita (n={n})"

    # Cancelar por la misma ruta.
    async with sm() as s:
        await run_agent(
            s, _ScriptedLLM([
                LLMResponse(content=None, finish_reason="tool_calls",
                            tool_calls=[_tc("x1", "cancel_appointment",
                                            {"date": target, "time_slot": "10:00"})]),
                LLMResponse(content="Tu cita quedó cancelada.",
                            finish_reason="stop"),
            ]),
            tenant_id, contact.id, "Cancela mi cita de mañana a las 10",
            embedder=FakeEmbedder(),
        )
        cancelled = (
            await s.execute(
                select(Appointment).where(Appointment.id == appt.id)
            )
        ).scalar_one()
    if cancelled.status != "cancelled":
        return False, "la cancelación no marcó status='cancelled' en BD"

    # Reprogramar por la misma ruta: agenda nueva y la mueve.
    d2 = (datetime.now(ZoneInfo(TENANT_TZ)) + timedelta(days=2)).date().isoformat()
    d3 = (datetime.now(ZoneInfo(TENANT_TZ)) + timedelta(days=3)).date().isoformat()
    async with sm() as s:
        await run_agent(
            s, _ScriptedLLM([
                LLMResponse(content=None, finish_reason="tool_calls",
                            tool_calls=[_tc("c1", "check_availability",
                                            {"date": d2, "time_slot": "11:00"})]),
                LLMResponse(content=None, finish_reason="tool_calls",
                            tool_calls=[_tc("c2", "book_appointment",
                                            {"date": d2, "time_slot": "11:00",
                                             "type": "consultation"})]),
                LLMResponse(content="Agendada.", finish_reason="stop"),
            ]),
            tenant_id, contact.id, "Agenda pasado mañana a las 11",
            embedder=FakeEmbedder(),
        )
        await run_agent(
            s, _ScriptedLLM([
                LLMResponse(content=None, finish_reason="tool_calls",
                            tool_calls=[_tc("r1", "reschedule_appointment",
                                            {"old_date": d2, "old_time_slot": "11:00",
                                             "new_date": d3, "new_time_slot": "12:00",
                                             "type": "consultation"})]),
                LLMResponse(content="Reprogramada.", finish_reason="stop"),
            ]),
            tenant_id, contact.id, "Cámbiala al día siguiente a las 12",
            embedder=FakeEmbedder(),
        )
        rows = (
            await s.execute(
                select(Appointment).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact.id,
                )
            )
        ).scalars().all()
    by_start = {a.start_at.strftime("%Y-%m-%d %H:%M"): a.status for a in rows}
    if by_start.get(f"{d2} 11:00") != "cancelled" or \
            by_start.get(f"{d3} 12:00") != "confirmed":
        return False, f"reprogramación inconsistente en BD: {by_start}"
    return True, (f"cita creada, reintento sin duplicar (1 sola), cancelada y "
                  f"reprogramada {d2} 11:00 -> {d3} 12:00")


async def _route_handoff(session, tenant_id) -> tuple[bool, str]:
    sm = db_mod.async_session_maker
    # 3a) Creación del Handoff por la ruta del engine.
    async with sm() as s:
        contact = await _get_or_create_contact(
            s, tenant_id, "521550001002", name="Paciente Urgencia")
        await s.commit()
        cid = contact.id
    async with sm() as s:
        reply = await run_agent(
            s, _UrgencyLLM(), tenant_id, cid, URGENCY_TEXT,
            embedder=FakeEmbedder(),
        )
        handoff = (
            await s.execute(
                select(Handoff).where(
                    Handoff.tenant_id == tenant_id,
                    Handoff.contact_id == cid,
                    Handoff.status == "open",
                ).order_by(Handoff.created_at.desc())
            )
        ).scalars().first()
        conv = await get_or_create_conversation(s, tenant_id, cid)
        mode = conv.mode
    if handoff is None:
        return False, "no se creó el Handoff en BD"
    if mode != MODE_HUMAN:
        return False, f"conversation.mode={mode!r}, se esperaba 'human'"
    if reply != HANDOFF_NOTICE:
        return False, "el bot no devolvió el aviso de escalación"

    # 3b) El bot deja de responder: probado con el drenador REAL. Contacto
    # nuevo por corrida (wa_id único) para no mezclar evidencia. Se da de
    # alta con consentimiento ANTES del primer mensaje: si no, la puerta
    # de privacidad (Fase 7c) enviaría el aviso en vez de escalar y la
    # ruta probaría el gate, no el silencio tras el handoff.
    stamp = uuid.uuid4().hex[:8]
    wa = f"52155{stamp}"
    async with sm() as s:
        await _get_or_create_contact(s, tenant_id, wa, name="Paciente Demo")
        await s.commit()
    llm_factory = lambda s_, t_, e_: _UrgencyLLM()  # noqa: E731

    def _change(wamid, text):
        return {
            "contacts": [{"profile": {"name": "Paciente Demo"}}],
            "messages": [{"from": wa, "id": wamid, "type": "text",
                          "text": {"body": text}}],
        }

    async with sm() as s:
        await enqueue_job(s, tenant_id, "DEMO_PNID", _change(f"demo-{stamp}-1", URGENCY_TEXT))
        await s.commit()
    stats1 = await drain_jobs(sm, llm_factory=llm_factory, dry_run=True)
    async with sm() as s:
        contact2 = (
            await s.execute(
                select(Contact).where(
                    Contact.tenant_id == tenant_id, Contact.wa_id == wa)
            )
        ).scalar_one()
        out1 = (
            await s.execute(
                select(func.count()).select_from(Message).where(
                    Message.tenant_id == tenant_id,
                    Message.contact_id == contact2.id,
                    Message.direction == "outbound",
                )
            )
        ).scalar()
        await enqueue_job(s, tenant_id, "DEMO_PNID",
                          _change(f"demo-{stamp}-2", "¿siguen ahí?"))
        await s.commit()
    stats2 = await drain_jobs(sm, llm_factory=llm_factory, dry_run=True)
    async with sm() as s:
        out2 = (
            await s.execute(
                select(func.count()).select_from(Message).where(
                    Message.tenant_id == tenant_id,
                    Message.contact_id == contact2.id,
                    Message.direction == "outbound",
                )
            )
        ).scalar()
    if stats1["done"] != 1:
        return False, f"el drenador no procesó el mensaje de urgencia: {stats1}"
    if out1 != 1:
        return False, f"se esperaban 1 respuesta (aviso) tras urgencia, hay {out1}"
    if stats2["done"] != 1:
        return False, f"el drenador no procesó el 2do mensaje: {stats2}"
    if out2 != out1:
        return False, (f"el bot RESPONDIÓ tras el handoff "
                       f"(outbound {out1} -> {out2}); debía guardar silencio")
    return True, ("Handoff creado, conversation.mode='human', y el drenador "
                  "silenció al bot en el siguiente mensaje")


async def _route_reminders(session, tenant_id, contact) -> tuple[bool, str]:
    sm = db_mod.async_session_maker
    # La ruta reserva "ahora + 2h" truncado al minuto: entre corridas
    # cercanas podría colisionar con la cita de la corrida anterior en el
    # índice único (tenant_id, start_at), así que se limpia primero
    # (mismo patrón que _cleanup_route2).
    async with sm() as s:
        await _cleanup_route2(s, tenant_id, contact.id)
    async with sm() as s:
        cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
        start = datetime.now(ZoneInfo(TENANT_TZ)) + timedelta(hours=2)
        res = await cal.book(
            str(contact.id),
            start.date().isoformat(),
            start.strftime("%H:%M"),
            "consultation",
            idempotency_key=f"demo-reminder-{uuid.uuid4().hex[:8]}",
        )
        if not res.get("ok"):
            return False, f"no se pudo crear la cita de prueba: {res}"
        await s.commit()

    async def _log_count():
        async with sm() as s:
            return (
                await s.execute(
                    select(func.count()).select_from(ReminderLog).where(
                        ReminderLog.tenant_id == tenant_id,
                        ReminderLog.contact_id == contact.id,
                    )
                )
            ).scalar()

    before = await _log_count()
    await scheduler_mod.run_once(dry_run=True)   # jamás llamar a Meta en la demo
    after_first = await _log_count()
    await scheduler_mod.run_once(dry_run=True)   # segunda corrida: no duplicar
    after_second = await _log_count()

    if after_first <= before:
        return False, "el scheduler no generó ningún reminder_log"
    if after_second != after_first:
        return False, (f"la segunda corrida duplicó recordatorios "
                       f"({after_first} -> {after_second})")
    return True, (f"{after_first - before} recordatorio(s) generados, "
                  f"0 duplicados en 2da corrida")


async def _run(reset: bool) -> int:
    print("=" * 64)
    print("DEMO LIAH — Consultorio médico (end-to-end, Fase 5)")
    print("=" * 64)
    t0 = time.time()
    results: list[tuple[str, bool, str]] = []

    print("[0/5] Base de datos demo")
    await _ensure_database(DEMO_DB_URL)
    _wire_db(DEMO_DB_URL)
    await _prepare_schema(reset)

    print("[1/5] Alta de la clínica (onboarding real)")
    async with db_mod.async_session_maker() as s:
        tenant_id = await _get_or_create_tenant(s)
        await _ensure_faq_chunk(s, tenant_id)
        await s.commit()

    print("[2/5] Ruta 1 — Conocimiento (precio en la RESPUESTA FINAL)")
    try:
        async with db_mod.async_session_maker() as s:
            contact = await _get_or_create_contact(
                s, tenant_id, "521550001001", name="Paciente Demo")
            await s.commit()
            ok, detail = await _route_knowledge(s, tenant_id, contact)
        results.append(("conocimiento", ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append(("conocimiento", False, f"{type(e).__name__}: {e}"))

    print("[3/5] Ruta 2 — Acción (agendar/cancelar/reprogramar por run_agent)")
    try:
        async with db_mod.async_session_maker() as s:
            contact = await _get_or_create_contact(
                s, tenant_id, "521550001001", name="Paciente Demo")
            await _cleanup_route2(s, tenant_id, contact.id)
            ok, detail = await _route_booking(s, tenant_id, contact)
        results.append(("acción/booking", ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append(("acción/booking", False, f"{type(e).__name__}: {e}"))

    print("[4/5] Ruta 3 — Handoff (urgencia -> humano + silencio)")
    try:
        async with db_mod.async_session_maker() as s:
            ok, detail = await _route_handoff(s, tenant_id)
        results.append(("handoff", ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append(("handoff", False, f"{type(e).__name__}: {e}"))

    print("[5/5] Ruta 4 — Recordatorios (idempotentes)")
    try:
        async with db_mod.async_session_maker() as s:
            contact = await _get_or_create_contact(
                s, tenant_id, "521550001003", name="Paciente Recordatorio")
            await s.commit()
            ok, detail = await _route_reminders(s, tenant_id, contact)
        results.append(("recordatorios", ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append(("recordatorios", False, f"{type(e).__name__}: {e}"))

    print(f"\nTiempo total: {time.time() - t0:.1f}s")
    return _report(results)


def main(argv=None) -> int:
    args = _parse_args(argv)
    reset = args.reset or os.getenv("LIAH_DEMO_RESET") == "1"
    if reset:
        print("ATENCIÓN: --reset borra el esquema de la BD demo "
              f"({urlparse(DEMO_DB_URL.replace('+asyncpg', '')).path}).")
    try:
        return asyncio.run(_run(reset))
    except Exception as e:  # noqa: BLE001
        print(f"\nDEMO FALLIDA (excepción no controlada): {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
