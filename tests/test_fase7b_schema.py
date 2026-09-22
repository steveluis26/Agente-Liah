"""Tests de Fase 7b: esquema de configuración (recursos, tipos de servicio,
términos de privacidad).

- Validación estricta del schema 1.1 (acepta/rechaza).
- Las 6 plantillas cargan con `load_template` y `list_templates` sin errores.
- El onboarding persiste resources/service_types/privacy_terms en la misma
  transacción.

Requieren Postgres + pgvector (los de BD). El engine de test se configura
en conftest.
"""
import os
from copy import deepcopy

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent.embedder import FakeEmbedder
from app.api.onboarding import onboard_tenant
from app.core import db as db_mod
from app.core.base import Base
from app.core.profile_schema import (
    PerfilGiro,
    list_templates,
    load_template,
)
from app.models import (
    Appointment,
    AppointmentResource,
    Contact,
    Resource,
    ServiceType,
    Tenant,
    TenantPrivacyTerms,
    WaitlistEntry,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLINIC_YAML = os.path.join(REPO_ROOT, "templates", "consultorio_medico.yaml")
PW = "Secret-12345678"

TEMPLATES_7B = (
    "consultorio_medico",
    "estetica",
    "escuela_privada",
    "academia_danza",
    "snacks_eventos",
    "espejo_magico",
)


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    url = os.getenv("TEST_DATABASE_URL", "").replace("+asyncpg", "")
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    finally:
        await conn.close()
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _clinic_data():
    import yaml

    return yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())


async def _onboard(
    session,
    slug: str,
    template: str = "consultorio_medico",
    overrides: dict | None = None,
    admin_email: str | None = None,
):
    return await onboard_tenant(
        session,
        template_name=template,
        slug=slug,
        nombre=None,
        overrides=overrides,
        admin_email=admin_email or f"{slug}@test.mx",
        admin_password=PW,
        embedder=FakeEmbedder(),
    )


# ── Schema 1.1: aceptación ──────────────────────────────────────────────


def test_schema_1_1_accepts_full_profile():
    p = load_template(CLINIC_YAML)
    assert p.schema_version == "1.1"
    assert len(p.resources) == 5
    assert len(p.service_types) == 4
    assert p.privacy_terms is not None
    assert p.privacy_terms.version == "1.0"
    # discriminación recurso-vs-tipo en el mismo perfil
    reqs = [r for st in p.service_types for r in st.recursos]
    assert any(r.recurso == "medico-general" for r in reqs)
    assert any(r.tipo == "room" and r.recurso is None for r in reqs)


def test_schema_1_0_without_new_sections_still_valid():
    """Los YAML 1.0 sin las secciones nuevas siguen validando."""
    data = _clinic_data()
    for key in ("resources", "service_types", "privacy_terms"):
        data.pop(key, None)
    data["schema_version"] = "1.0"
    p = PerfilGiro(**data)
    assert p.schema_version == "1.0"
    assert p.resources == []
    assert p.service_types == []
    assert p.privacy_terms is None


def test_traslado_variants_validate():
    data = _clinic_data()
    # fixed con fixed_min: válido
    data["service_types"][0]["traslado"] = {"modo": "fixed", "fixed_min": 30}
    PerfilGiro(**data)
    # per_zone con zonas: válido
    data["service_types"][0]["traslado"] = {
        "modo": "per_zone",
        "default_min": 45,
        "zonas": {"misma_ciudad": 20, "otra_ciudad": 60},
    }
    p = PerfilGiro(**data)
    t = p.service_types[0].traslado
    assert t.modo == "per_zone"
    assert t.zonas["otra_ciudad"] == 60
    # none explícito: válido
    data["service_types"][0]["traslado"] = {"modo": "none"}
    PerfilGiro(**data)


# ── Schema 1.1: rechazos ────────────────────────────────────────────────


def test_rejects_duplicate_resource_slugs():
    data = _clinic_data()
    data["resources"].append(deepcopy(data["resources"][0]))
    with pytest.raises(Exception, match="slug duplicado"):
        PerfilGiro(**data)


def test_rejects_duplicate_service_type_slugs():
    data = _clinic_data()
    data["service_types"].append(deepcopy(data["service_types"][0]))
    with pytest.raises(Exception, match="slug duplicado"):
        PerfilGiro(**data)


def test_rejects_reference_to_unknown_resource():
    data = _clinic_data()
    data["service_types"][0]["recursos"] = [{"recurso": "recurso-fantasma"}]
    with pytest.raises(Exception, match="inexistente"):
        PerfilGiro(**data)


def test_rejects_fixed_traslado_without_fixed_min():
    data = _clinic_data()
    data["service_types"][0]["traslado"] = {"modo": "fixed"}
    with pytest.raises(Exception, match="fixed_min"):
        PerfilGiro(**data)


def test_rejects_per_zone_traslado_without_zonas():
    data = _clinic_data()
    data["service_types"][0]["traslado"] = {"modo": "per_zone"}
    with pytest.raises(Exception, match="zonas"):
        PerfilGiro(**data)


def test_rejects_recurso_and_tipo_together():
    """Discriminación: {"recurso", "tipo"} juntos es error."""
    data = _clinic_data()
    data["service_types"][0]["recursos"] = [
        {"recurso": "medico-general", "tipo": "room"}
    ]
    with pytest.raises(Exception, match="excluyentes"):
        PerfilGiro(**data)


def test_rejects_recurso_with_neither():
    """Discriminación: sin "recurso" ni "tipo" es error."""
    data = _clinic_data()
    data["service_types"][0]["recursos"] = [{"cantidad": 2}]
    with pytest.raises(Exception, match="excluyentes"):
        PerfilGiro(**data)


def test_rejects_empty_recursos_list():
    data = _clinic_data()
    data["service_types"][0]["recursos"] = []
    with pytest.raises(Exception, match="recursos"):
        PerfilGiro(**data)


def test_rejects_bad_values():
    data = _clinic_data()
    bad = deepcopy(data)
    bad["service_types"][0]["duracion_min"] = 0
    with pytest.raises(Exception):
        PerfilGiro(**bad)
    bad = deepcopy(data)
    bad["resources"][0]["capacidad"] = 0
    with pytest.raises(Exception):
        PerfilGiro(**bad)
    bad = deepcopy(data)
    bad["resources"][0]["tipo"] = "nave-espacial"
    with pytest.raises(Exception):
        PerfilGiro(**bad)


# ── Plantillas ──────────────────────────────────────────────────────────


def test_all_six_templates_load_and_validate():
    for name in TEMPLATES_7B:
        perfil = load_template(f"templates/{name}.yaml")
        assert perfil.schema_version == "1.1", name
        assert perfil.giro == name, name
        assert perfil.service_types, f"{name} sin service_types"
        assert perfil.privacy_terms is not None, f"{name} sin privacy_terms"
        assert perfil.privacy_terms.texto.strip(), name
        # los recursos referenciados por slug existen (la validación
        # cruzada ya lo garantiza, esto documenta la intención)
        conocidos = {r.slug for r in perfil.resources}
        for st in perfil.service_types:
            for req in st.recursos:
                if req.recurso is not None:
                    assert req.recurso in conocidos, (name, st.slug)


def test_mobile_templates_use_per_zone_traslado():
    """Negocios móviles: traslado per_zone con las tres zonas."""
    for name in ("snacks_eventos", "espejo_magico"):
        perfil = load_template(f"templates/{name}.yaml")
        assert any(r.movilidad == "mobile" for r in perfil.resources), name
        assert any(r.tipo == "equipment" for r in perfil.resources), name
        for st in perfil.service_types:
            t = st.traslado
            assert t.modo == "per_zone", (name, st.slug)
            assert set(t.zonas) >= {"misma_sede", "misma_ciudad", "otra_ciudad"}
            assert st.buffers_min.setup > 0
            assert st.buffers_min.teardown > 0


def test_list_templates_no_errors():
    rows = {r["template"]: r for r in list_templates()}
    for name in TEMPLATES_7B:
        assert name in rows, f"falta en list_templates(): {name}"
        assert "error" not in rows[name], rows[name].get("error")
        assert rows[name]["schema_version"] == "1.1"
        assert rows[name]["tipos_servicio"] > 0
        assert rows[name]["privacy_terms"] is True


# ── Onboarding persiste el esquema ──────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_persists_resources_service_types_privacy_terms():
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-f7b")
    assert result["summary"]["recursos"] == 5
    assert result["summary"]["tipos_servicio"] == 4
    assert result["summary"]["privacy_terms"] == "1.0"

    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-f7b"))
        ).scalar_one()

        recursos = (
            await s.execute(
                select(Resource).where(Resource.tenant_id == tenant.id)
            )
        ).scalars().all()
        assert {r.slug for r in recursos} == {
            "consultorio-1", "consultorio-2", "medico-general",
            "pediatra", "electrocardiografo",
        }
        por_tipo = {r.tipo for r in recursos}
        assert {"room", "specialist", "equipment"} <= por_tipo
        assert all(r.movilidad == "fixed" for r in recursos)
        esp = next(r for r in recursos if r.slug == "pediatra")
        assert esp.especialidad == "pediatría"

        sts = (
            await s.execute(
                select(ServiceType).where(ServiceType.tenant_id == tenant.id)
            )
        ).scalars().all()
        assert {t.slug for t in sts} == {
            "consulta-medicina-general", "consulta-pediatria",
            "electrocardiograma", "chequeo-anual",
        }
        ecg = next(t for t in sts if t.slug == "electrocardiograma")
        assert ecg.duracion_min == 20
        # snapshot JSONB del perfil
        assert {"recurso": "electrocardiografo"} in ecg.recursos_requeridos
        assert {"tipo": "specialist", "cantidad": 1} in ecg.recursos_requeridos
        assert ecg.buffers == {"setup": 5, "teardown": 5}
        assert ecg.traslado == {
            "modo": "none", "fixed_min": None,
            "default_min": 60, "zonas": {},
        }

        pt = (
            await s.execute(
                select(TenantPrivacyTerms).where(
                    TenantPrivacyTerms.tenant_id == tenant.id
                )
            )
        ).scalar_one()
        assert pt.version == "1.0"
        assert "Clínica Ejemplo Norte" in pt.titulo
        assert len(pt.texto) > 20


@pytest.mark.asyncio
async def test_onboard_mobile_template_persists_per_zone():
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "snacks-f7b", template="snacks_eventos")
    assert result["summary"]["recursos"] == 5
    assert result["summary"]["tipos_servicio"] == 3

    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "snacks-f7b"))
        ).scalar_one()
        st = (
            await s.execute(
                select(ServiceType).where(
                    ServiceType.tenant_id == tenant.id,
                    ServiceType.slug == "barra-esquites-evento",
                )
            )
        ).scalar_one()
        assert st.traslado["modo"] == "per_zone"
        assert st.traslado["zonas"]["otra_ciudad"] == 90
        assert st.buffers == {"setup": 60, "teardown": 45}
        rec = (
            await s.execute(
                select(Resource).where(
                    Resource.tenant_id == tenant.id,
                    Resource.slug == "barra-esquites",
                )
            )
        ).scalar_one()
        assert rec.movilidad == "mobile"
        assert rec.tipo == "equipment"


@pytest.mark.asyncio
async def test_onboard_without_privacy_terms_creates_no_row():
    """Si privacy_terms es None, no se crea la fila."""
    async with db_mod.async_session_maker() as s:
        result = await _onboard(
            s, "clinica-sin-pt", overrides={"privacy_terms": None}
        )
    assert result["summary"]["privacy_terms"] is None
    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-sin-pt"))
        ).scalar_one()
        n = await s.scalar(
            select(func.count()).select_from(TenantPrivacyTerms).where(
                TenantPrivacyTerms.tenant_id == tenant.id
            )
        )
        assert n == 0


@pytest.mark.asyncio
async def test_appointment_service_type_venue_and_contact_privacy_version():
    """Columnas nuevas: Appointment.service_type_slug/venue,
    Contact.privacy_terms_version; y tablas AppointmentResource/WaitlistEntry."""
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-cols", template="snacks_eventos")
        import uuid as uuid_mod

        tid = uuid_mod.UUID(result["tenant_id"])
        contact = Contact(
            tenant_id=tid, wa_id="521555007700", name="Cliente Evento",
            consent_status="granted", privacy_terms_version="1.0",
        )
        s.add(contact)
        await s.flush()
        assert contact.privacy_terms_version == "1.0"

        from datetime import datetime, timedelta

        appt = Appointment(
            tenant_id=tid, contact_id=contact.id, type="other",
            start_at=datetime.now() + timedelta(days=7),
            status="confirmed",
            service_type_slug="barra-esquites-evento",
            venue="Salón Ejemplo, Veracruz",
        )
        s.add(appt)
        await s.flush()
        assert appt.service_type_slug == "barra-esquites-evento"
        assert appt.venue == "Salón Ejemplo, Veracruz"

        recurso = (
            await s.execute(
                select(Resource).where(
                    Resource.tenant_id == tid, Resource.slug == "barra-esquites"
                )
            )
        ).scalar_one()
        s.add(AppointmentResource(
            tenant_id=tid, appointment_id=appt.id, resource_id=recurso.id
        ))
        s.add(WaitlistEntry(
            tenant_id=tid, contact_id=contact.id,
            service_type_slug="barra-nachos-evento",
        ))
        await s.commit()

        n_ar = await s.scalar(
            select(func.count()).select_from(AppointmentResource)
        )
        assert n_ar == 1
        wl = (
            await s.execute(select(WaitlistEntry).where(
                WaitlistEntry.tenant_id == tid
            ))
        ).scalar_one()
        assert wl.status == "waiting"
        assert wl.service_type_slug == "barra-nachos-evento"
        assert wl.current_appointment_id is None
