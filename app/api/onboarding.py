"""Alta de clientes desde plantilla (Fase 4).

POST /api/v1/admin/tenants/onboard (solo platform_admin):

    {"template": "consultorio_medico", "slug": "clinica-norte",
     "nombre": "Clínica Norte Real", "overrides": {...},
     "admin_email": "admin@clinica.mx", "admin_password": "..."}

Crea en UNA transacción: tenant (+ API key), tenant_configs (model_routing
comercial por default), automation_rules, templates (HSM), conocimiento
semilla ingerido con el embedder del tenant, y el tenant_admin del panel.
Si cualquier paso falla -> rollback completo, nada persiste.

La password viaja solo en el body de la request autenticada (transitoria:
se hashea con PBKDF2 y jamás se guarda en claro ni se devuelve).
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import build_embedder_for_tenant
from app.agent.ports import EmbedderPort
from app.agent.rag import ingest_knowledge
from app.api.admin import ModelRoutingUpdate
from app.core.audit import log_event
from app.core.auth import (
    CurrentUser,
    generate_api_key,
    hash_api_key,
    require_platform_admin,
)
from app.core.db import get_session
from app.core.profile_schema import (
    TEMPLATES_DIR,
    PerfilGiro,
    apply_overrides,
    list_templates,
    load_template,
)
from app.models import (
    AutomationRule,
    PlatformUser,
    Template,
    Tenant,
    TenantConfig,
)
from app.models.platform_users import ROLE_TENANT_ADMIN, hash_password

router = APIRouter(prefix="/api/v1/admin", tags=["admin-onboarding"])

# Ruteo comercial por defecto: el cliente paga su propia API key de OpenAI
# (decisión Fase 2). Salvo override explícito en el perfil o la request.
DEFAULT_COMMERCIAL_ROUTING = {
    "llm_provider": "openai",
    "llm_model": "gpt-4o-mini",
    "embedder": "openai",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MIN_ADMIN_PASSWORD_LEN = 12


class OnboardingError(Exception):
    """Fallo esperado del alta (se mapea a 4xx). `code` decide el status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OnboardRequest(BaseModel):
    template: str = Field(min_length=1, max_length=64)
    slug: str = Field(min_length=2, max_length=64)
    nombre: str | None = Field(default=None, max_length=160)
    overrides: dict = Field(default_factory=dict)
    admin_email: str
    admin_password: str


class OnboardResponse(BaseModel):
    tenant_id: str
    slug: str
    nombre: str
    api_key: str  # se muestra UNA vez (igual que POST /tenants)
    admin_email: str
    template: str
    giro: str
    summary: dict


def _validate_identity(slug: str, email: str, password: str) -> tuple[str, str]:
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise OnboardingError(
            "invalid_slug",
            "slug inválido: solo minúsculas, dígitos y guiones",
        )
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise OnboardingError("invalid_email", "admin_email inválido")
    if len(password) < MIN_ADMIN_PASSWORD_LEN:
        raise OnboardingError(
            "weak_password",
            f"admin_password debe tener al menos {MIN_ADMIN_PASSWORD_LEN} caracteres",
        )
    return slug, email


async def onboard_tenant(
    session: AsyncSession,
    *,
    template_name: str,
    slug: str,
    nombre: str | None,
    overrides: dict | None,
    admin_email: str,
    admin_password: str,
    embedder: EmbedderPort | None = None,
) -> dict:
    """Alta completa de un cliente desde un perfil de `templates/`.

    Todo ocurre en la transacción de `session`: esta función hace flush y UN
    commit al final; ante cualquier error hace rollback y lanza
    OnboardingError (esperado) o la excepción original (inesperado).

    `embedder`: inyección para tests. En producción se construye con el
    factory del tenant (`build_embedder_for_tenant`) desde el model_routing
    resultante.
    """
    # 1) Plantilla: existe, YAML válido, cumple el schema.
    template_path = (TEMPLATES_DIR / f"{template_name}.yaml").resolve()
    if TEMPLATES_DIR not in template_path.parents:
        raise OnboardingError("invalid_template", "nombre de plantilla inválido")
    try:
        perfil_dict = load_template(template_path).model_dump()
    except ValueError as e:
        raise OnboardingError("invalid_template", str(e))

    # 2) Overrides del operador -> revalidar el perfil completo.
    try:
        perfil_dict = apply_overrides(perfil_dict, overrides)
        perfil = PerfilGiro(**perfil_dict)
    except ValueError as e:
        raise OnboardingError("invalid_overrides", str(e))

    slug, admin_email = _validate_identity(slug, admin_email, admin_password)

    try:
        # 3) Unicidad ANTES de crear nada (el rollback también cubriría, pero
        #    fallar temprano da un 409 limpio).
        if (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none() is not None:
            raise OnboardingError("slug_exists", f"slug '{slug}' ya existe")
        if (
            await session.execute(
                select(PlatformUser).where(PlatformUser.email == admin_email)
            )
        ).scalar_one_or_none() is not None:
            raise OnboardingError(
                "email_exists", f"admin_email '{admin_email}' ya está en uso"
            )

        # 4) Tenant + API key (se muestra una sola vez).
        api_key, salt = generate_api_key()
        tenant = Tenant(
            slug=slug,
            name=nombre or perfil.nombre,
            business_type=perfil.giro,
            timezone=perfil.timezone,
            api_key_hash=hash_api_key(api_key, salt),
            api_key_salt=salt,
        )
        session.add(tenant)
        await session.flush()

        # 5) Config: model_routing comercial por default, salvo override.
        routing = dict(DEFAULT_COMMERCIAL_ROUTING)
        if perfil.model_routing:
            routing.update(perfil.model_routing)
        routing = ModelRoutingUpdate(**routing).model_dump(exclude_none=True)
        session.add(
            TenantConfig(
                tenant_id=tenant.id,
                system_prompt=perfil.system_prompt,
                tone=perfil.tono,
                business_hours=perfil.horarios,
                lfpdp_consent_required=perfil.politicas.consentimiento_recordatorios,
                model_routing=routing,
                extra={
                    "enabled_tools": perfil.herramientas_habilitadas,
                    "giro": perfil.giro,
                    "template": template_name,
                    "template_schema_version": perfil.schema_version,
                    "temas_sensibles": perfil.politicas.temas_sensibles,
                    "escalamiento": perfil.politicas.escalamiento,
                },
            )
        )

        # 6) Reglas de automatización.
        for regla in perfil.reglas:
            session.add(
                AutomationRule(
                    tenant_id=tenant.id,
                    type=regla.tipo,
                    enabled=regla.enabled,
                    params=regla.params,
                )
            )

        # 7) Plantillas HSM (status pending: requieren aprobación de Meta).
        for tpl in perfil.plantillas_hsm:
            session.add(
                Template(
                    tenant_id=tenant.id,
                    name=tpl.nombre,
                    category=tpl.categoria,
                    language=tpl.idioma,
                    body=tpl.body,
                    variables=tpl.variables,
                    status="pending",
                )
            )

        # 8) Conocimiento semilla con el embedder del tenant (sin commit:
        #    viaja en la transacción del onboarding).
        if embedder is None:
            routing_for_factory = dict(routing)
            routing_for_factory["_tenant_slug"] = slug
            embedder = build_embedder_for_tenant(routing_for_factory)
        chunk_count = 0
        for item in perfil.conocimiento_semilla:
            await ingest_knowledge(
                session, tenant.id, item.titulo, item.contenido, embedder,
                commit=False,
            )

        # 9) tenant_admin del panel (password hasheado, jamás en claro).
        session.add(
            PlatformUser(
                email=admin_email,
                password_hash=hash_password(admin_password),
                role=ROLE_TENANT_ADMIN,
                tenant_id=tenant.id,
            )
        )

        await log_event(
            session, tenant.id, "tenant.onboarded",
            {"template": template_name, "giro": perfil.giro, "slug": slug},
        )

        await session.commit()
    except OnboardingError:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise

    return {
        "tenant_id": str(tenant.id),
        "slug": slug,
        "nombre": tenant.name,
        "api_key": api_key,
        "admin_email": admin_email,
        "template": template_name,
        "giro": perfil.giro,
        "summary": {
            "reglas": len(perfil.reglas),
            "plantillas_hsm": len(perfil.plantillas_hsm),
            "conocimiento_items": len(perfil.conocimiento_semilla),
        },
    }


def _http_status(code: str) -> int:
    return {
        "slug_exists": status.HTTP_409_CONFLICT,
        "email_exists": status.HTTP_409_CONFLICT,
    }.get(code, status.HTTP_422_UNPROCESSABLE_ENTITY)


@router.get("/templates", response_model=list[dict])
async def api_list_templates(
    _admin: CurrentUser = Depends(require_platform_admin),
):
    """Giros disponibles en `templates/*.yaml` (para el panel/CLI)."""
    return list_templates()


@router.post(
    "/tenants/onboard",
    response_model=OnboardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_onboard_tenant(
    body: OnboardRequest,
    _admin: CurrentUser = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    """Alta de cliente desde plantilla. SOLO platform_admin.

    La password del tenant_admin viaja solo en este body autenticado: se
    hashea (PBKDF2) y jamás se persiste en claro ni se devuelve.
    """
    try:
        result = await onboard_tenant(
            session,
            template_name=body.template,
            slug=body.slug,
            nombre=body.nombre,
            overrides=body.overrides,
            admin_email=body.admin_email,
            admin_password=body.admin_password,
        )
    except OnboardingError as e:
        raise HTTPException(status_code=_http_status(e.code), detail=str(e))
    return OnboardResponse(**result)
