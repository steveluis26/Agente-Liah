"""Panel de operador (Fase 3): API JSON bajo /api/v1/admin.

- POST /auth/login: login humano (email+password) -> JWT. También fija la
  cookie httpOnly `liah_admin_token` para la UI server-rendered (/admin).
- Bandeja de handoff: listar/tomar/resolver/devolver al bot.
- Config por tenant: GET/PUT (valida model_routing contra el factory de
  Fase 2; NUNCA expone ni acepta secretos).
- Métricas: resolución automática, transferencias, tiempo a primera
  respuesta, acciones exitosas, costo por conversación.

Autenticación: `Authorization: Bearer <JWT>` o cookie httpOnly (misma
verificación). Roles: platform_admin (todo), tenant_admin/tenant_agent
(solo su tenant). Las API keys por tenant (X-Tenant-API-Key) siguen
sirviendo para webhook/ingesta, no para este panel.
"""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    ADMIN_JWT_COOKIE,
    CurrentUser,
    authenticate_platform_user,
    create_access_token,
    get_current_user,
    require_platform_admin,
    require_tenant_access,
    require_tenant_role,
)
from app.core.config import get_settings
from app.core.db import get_session
from app.models import (
    ActionLog,
    Appointment,
    Campaign,
    CampaignSend,
    Contact,
    ContactTag,
    Conversation,
    Handoff,
    Message,
    Template,
    Tenant,
    TenantConfig,
    UsageMonthly,
    UsageRecord,
)
from app.models.conversations import (
    MODE_AI,
    MODE_HUMAN,
    MODE_RESOLVED,
    set_conversation_mode,
)
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_TENANT_AGENT,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

PANEL_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)
HANDOFF_OPEN_STATES = ("open", "taken")


# ── Auth ────────────────────────────────────────────────────────────────


class LoginBody(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str
    tenant_id: str | None


def _set_auth_cookie(response: Response, token: str) -> None:
    """Cookie httpOnly para la UI (/admin). `secure` solo en producción."""
    response.set_cookie(
        ADMIN_JWT_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().app_env == "production",
        max_age=get_settings().liah_jwt_expire_minutes * 60,
        path="/",
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginBody,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate_platform_user(session, body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )
    token = create_access_token(user.id, user.role, user.tenant_id)
    _set_auth_cookie(response, token)
    return LoginResponse(
        access_token=token,
        email=user.email,
        role=user.role,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(ADMIN_JWT_COOKIE, path="/")
    return {"status": "logged_out"}


# ── Tenants (listado para el panel) ───────────────────────────────────────


@router.get("/tenants")
async def list_tenants(
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Lista tenants visibles para el operador (para pickers de la UI)."""
    q = select(Tenant).order_by(Tenant.created_at.desc())
    if not user.is_platform_admin:
        q = q.where(Tenant.id == user.tenant_id)
    tenants = (await session.execute(q)).scalars().all()
    return [
        {"id": str(t.id), "slug": t.slug, "name": t.name,
         "business_type": t.business_type, "timezone": t.timezone}
        for t in tenants
    ]


# ── Bandeja de handoff ────────────────────────────────────────────────────


def _handoff_scope(user: CurrentUser):
    """Filtro de tenant según el rol (None = sin filtro: platform_admin)."""
    if user.is_platform_admin:
        return None
    return Handoff.tenant_id == user.tenant_id


@router.get("/handoffs")
async def list_handoffs(
    status: str | None = Query(default=None, description="open|taken|resolved"),
    tenant_id: uuid.UUID | None = Query(default=None),
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Bandeja: platform_admin ve todos (filtrable por tenant); los roles de
    tenant solo ven los de su tenant."""
    if status is not None and status not in ("open", "taken", "resolved"):
        raise HTTPException(status_code=422, detail="status inválido")
    q = (
        select(Handoff, Contact.wa_id, Contact.name)
        .join(Contact, Contact.id == Handoff.contact_id)
        .order_by(Handoff.created_at.desc())
    )
    scope = _handoff_scope(user)
    if scope is not None:
        q = q.where(scope)
    if tenant_id is not None:
        require_tenant_access(tenant_id, user)
        q = q.where(Handoff.tenant_id == tenant_id)
    if status:
        q = q.where(Handoff.status == status)
    rows = (await session.execute(q)).all()
    return [
        {
            "id": str(h.id),
            "tenant_id": str(h.tenant_id),
            "contact_id": str(h.contact_id),
            "contact_wa_id": wa_id,
            "contact_name": name,
            "reason": h.reason,
            "status": h.status,
            "taken_by": h.taken_by,
            "resolution_note": h.resolution_note,
            "created_at": h.created_at.isoformat(),
        }
        for h, wa_id, name in rows
    ]


async def _get_scoped_handoff(
    session: AsyncSession, handoff_id: uuid.UUID, user: CurrentUser
) -> Handoff:
    h = await session.get(Handoff, handoff_id)
    if h is None:
        raise HTTPException(status_code=404, detail="handoff no encontrado")
    require_tenant_access(h.tenant_id, user)
    return h


@router.post("/handoffs/{handoff_id}/take")
async def take_handoff(
    handoff_id: uuid.UUID,
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)
    ),
    session: AsyncSession = Depends(get_session),
):
    h = await _get_scoped_handoff(session, handoff_id, user)
    if h.status != "open":
        raise HTTPException(
            status_code=409, detail=f"solo se puede tomar un handoff 'open' ({h.status})"
        )
    h.status = "taken"
    h.taken_by = user.email
    await session.commit()
    return {"id": str(h.id), "status": h.status, "taken_by": h.taken_by}


class ResolveBody(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.post("/handoffs/{handoff_id}/resolve")
async def resolve_handoff(
    handoff_id: uuid.UUID,
    body: ResolveBody,
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Resuelve el handoff con nota del operador y cierra la conversación
    (mode → resolved): el episodio queda cerrado para métricas."""
    h = await _get_scoped_handoff(session, handoff_id, user)
    if h.status == "resolved":
        raise HTTPException(status_code=409, detail="el handoff ya está resuelto")
    h.status = "resolved"
    h.resolution_note = body.note or None
    await set_conversation_mode(
        session, h.tenant_id, h.contact_id, MODE_RESOLVED
    )
    await session.commit()
    return {"id": str(h.id), "status": h.status}


@router.post("/handoffs/{handoff_id}/return-to-bot")
async def return_to_bot(
    handoff_id: uuid.UUID,
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Devuelve la conversación al bot (mode → ai) y cierra el handoff si
    seguía abierto/tomado. El drenador vuelve a responder."""
    h = await _get_scoped_handoff(session, handoff_id, user)
    if h.status in HANDOFF_OPEN_STATES:
        h.status = "resolved"
        h.resolution_note = (h.resolution_note or "") + " [devuelto al bot]"
        h.resolution_note = h.resolution_note.strip()
    await set_conversation_mode(session, h.tenant_id, h.contact_id, MODE_AI)
    await session.commit()
    return {"id": str(h.id), "handoff_status": h.status, "conversation_mode": MODE_AI}


# ── Config por tenant ─────────────────────────────────────────────────────
#
# NUNCA se exponen ni aceptan secretos por aquí: TenantConfig no tiene campos
# de secretos (las API keys viven hasheadas en tenants; los tokens de canal
# viven como secret_ref). Si algún día se agrega un campo secreto al modelo,
# hay que excluirlo explícitamente en `_config_public()`.


class ModelRoutingUpdate(BaseModel):
    """model_routing validado contra lo que consume el factory de Fase 2
    (app/agent/engine.py::build_llm_for_tenant / build_embedder_for_tenant).

    `extra="forbid"`: una clave desconocida es 422, no se guarda en silencio.
    """

    model_config = {"extra": "forbid"}

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_max_tokens: int | None = None
    llm_temperature: float | None = None
    embedder: str | None = None
    ollama_model: str | None = None
    ollama_base_url: str | None = None
    ollama_embed_model: str | None = None
    tier: str | None = None

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v):
        if v is not None and v.lower() not in ("openai", "ollama"):
            raise ValueError("llm_provider válido: 'openai' | 'ollama'")
        return v.lower() if v else v

    @field_validator("embedder")
    @classmethod
    def _embedder(cls, v):
        if v is not None and v.lower() not in ("openai", "ollama", "fake"):
            raise ValueError("embedder válido: 'openai' | 'ollama' | 'fake'")
        return v.lower() if v else v

    @field_validator("llm_max_tokens")
    @classmethod
    def _max_tokens(cls, v):
        if v is not None and not (1 <= v <= 100_000):
            raise ValueError("llm_max_tokens debe estar entre 1 y 100000")
        return v

    @field_validator("llm_temperature")
    @classmethod
    def _temperature(cls, v):
        if v is not None and not (0.0 <= v <= 2.0):
            raise ValueError("llm_temperature debe estar entre 0.0 y 2.0")
        return v

    @field_validator("llm_model", "ollama_model", "ollama_base_url",
                     "ollama_embed_model", "tier")
    @classmethod
    def _non_empty(cls, v):
        if v is not None and not v.strip():
            raise ValueError("no puede ser vacío")
        return v


WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


def _validate_business_hours(value: dict) -> dict:
    """Horarios como {dia: {open: "HH:MM", close: "HH:MM"} | "closed"}."""
    import re

    if not isinstance(value, dict):
        raise ValueError("business_hours debe ser un objeto")
    hhmm = re.compile(r"^\d{2}:\d{2}$")
    for day, spec in value.items():
        if day not in WEEKDAYS:
            raise ValueError(f"día inválido: {day!r}")
        if spec == "closed":
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"{day}: debe ser {{open, close}} o 'closed'")
        for key in ("open", "close"):
            t = spec.get(key)
            if not isinstance(t, str) or not hhmm.match(t):
                raise ValueError(f"{day}.{key}: hora inválida (HH:MM)")
    return value


class ConfigUpdate(BaseModel):
    """PUT parcial: solo los campos presentes se actualizan.

    `model_routing`, cuando se envía, REEMPLAZA el dict completo (validado).
    """

    model_config = {"extra": "forbid"}

    system_prompt: str | None = None
    tone: str | None = Field(default=None, max_length=40)
    business_hours: dict | None = None
    handoff_phone: str | None = Field(default=None, max_length=20)
    lfpdp_consent_required: bool | None = None
    privacy_notice: str | None = None
    model_routing: ModelRoutingUpdate | None = None
    extra: dict | None = None

    @field_validator("business_hours")
    @classmethod
    def _hours(cls, v):
        return _validate_business_hours(v) if v is not None else v

    @field_validator("extra")
    @classmethod
    def _extra(cls, v):
        if v is not None and not isinstance(v, dict):
            raise ValueError("extra debe ser un objeto")
        return v

    @model_validator(mode="after")
    def _no_empty_system_prompt(self):
        if self.system_prompt is not None and not self.system_prompt.strip():
            raise ValueError("system_prompt no puede ser vacío")
        return self


def _config_public(cfg: TenantConfig, tenant: Tenant) -> dict:
    return {
        "tenant_id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "timezone": tenant.timezone,
        "system_prompt": cfg.system_prompt,
        "tone": cfg.tone,
        "business_hours": cfg.business_hours,
        "handoff_phone": cfg.handoff_phone,
        "lfpdp_consent_required": cfg.lfpdp_consent_required,
        "privacy_notice": cfg.privacy_notice,
        "model_routing": cfg.model_routing,
        "extra": cfg.extra,
    }


async def _get_scoped_config(
    session: AsyncSession, tenant_id: uuid.UUID, user: CurrentUser
) -> tuple[TenantConfig, Tenant]:
    require_tenant_access(tenant_id, user)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant no encontrado")
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=404, detail="config no encontrada")
    return cfg, tenant


@router.get("/tenants/{tenant_id}/config")
async def get_tenant_config(
    tenant_id: uuid.UUID,
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Config del tenant. Sin secretos: este endpoint jamás devuelve valores
    de API keys ni tokens (no existen en TenantConfig por diseño)."""
    cfg, tenant = await _get_scoped_config(session, tenant_id, user)
    return _config_public(cfg, tenant)


@router.put("/tenants/{tenant_id}/config")
async def update_tenant_config(
    tenant_id: uuid.UUID,
    body: ConfigUpdate,
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    cfg, tenant = await _get_scoped_config(session, tenant_id, user)
    data = body.model_dump(exclude_unset=True)
    routing = data.pop("model_routing", None)
    for field, value in data.items():
        setattr(cfg, field, value)
    if routing is not None:
        # Reemplazo total del dict (ya validado por el schema).
        cfg.model_routing = routing
    await session.commit()
    return _config_public(cfg, tenant)


# ── Campañas y avisos (Fase 6) ─────────────────────────────────────────
#
# Solo platform_admin y tenant_admin crean/lanzan campañas (el dinero de
# Meta y el riesgo de baneo lo decide quien opera el negocio, no el agente).
# Los templates solo se envían si están APROBADOS por Meta; el estado se
# marca en el panel (Campañas → Plantillas) tras la aprobación en el
# Business Manager (ver docs/GUIA_ALTA.md: dependencia del cliente).


from app.marketing import campaigns as campaign_svc  # noqa: E402
from app.marketing.optin import OPTIN_SOURCES, set_opt_in  # noqa: E402
from app.core.audit import log_event  # noqa: E402

CAMPAIGN_ADMIN_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN)
TEMPLATE_STATUSES = ("pending", "approved", "rejected")


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(default="promo")  # promo|notice
    template_name: str = Field(min_length=1, max_length=80)
    params: dict = Field(default_factory=dict)
    segment: dict = Field(default_factory=dict)
    scheduled_at: datetime | None = None

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v not in ("promo", "notice"):
            raise ValueError("type válido: 'promo' | 'notice'")
        return v


def _campaign_public(c: Campaign) -> dict:
    return {
        "id": str(c.id),
        "tenant_id": str(c.tenant_id),
        "name": c.name,
        "type": c.type,
        "template_name": c.template_name,
        "params": c.params,
        "segment": c.segment,
        "status": c.status,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "launched_at": c.launched_at.isoformat() if c.launched_at else None,
        "total_targets": c.total_targets,
        "last_error": c.last_error,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _get_scoped_campaign(
    session: AsyncSession, campaign_id: uuid.UUID, user: CurrentUser
) -> Campaign:
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="campaña no encontrada")
    require_tenant_access(c.tenant_id, user)
    return c


@router.post("/tenants/{tenant_id}/campaigns", status_code=201)
async def create_campaign(
    tenant_id: uuid.UUID,
    body: CampaignCreate,
    user: CurrentUser = Depends(require_tenant_role(*CAMPAIGN_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Crea una campaña en draft (o scheduled si trae scheduled_at).

    NO valida aprobación de Meta aquí: eso se valida en el launch (422 con
    mensaje accionable si la plantilla no está aprobada).
    """
    require_tenant_access(tenant_id, user)
    try:
        c = await campaign_svc.create_campaign(
            session, tenant_id,
            name=body.name, type=body.type, template_name=body.template_name,
            params=body.params, segment=body.segment,
            scheduled_at=body.scheduled_at, created_by=user.email,
        )
    except campaign_svc.CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return _campaign_public(c)


@router.get("/tenants/{tenant_id}/campaigns")
async def list_campaigns(
    tenant_id: uuid.UUID,
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    require_tenant_access(tenant_id, user)
    rows = (
        await session.execute(
            select(Campaign)
            .where(Campaign.tenant_id == tenant_id)
            .order_by(Campaign.created_at.desc())
        )
    ).scalars().all()
    return [_campaign_public(c) for c in rows]


@router.get("/tenants/{tenant_id}/campaigns/{campaign_id}")
async def get_campaign(
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Detalle + métricas (enviados/entregados/leídos/fallidos, tasa de
    lectura, costo) + muestra de envíos."""
    require_tenant_access(tenant_id, user)
    c = await _get_scoped_campaign(session, campaign_id, user)
    metrics = await campaign_svc.campaign_metrics(session, tenant_id, c.id)
    sends = (
        await session.execute(
            select(CampaignSend, Contact.wa_id, Contact.name)
            .join(Contact, Contact.id == CampaignSend.contact_id)
            .where(CampaignSend.campaign_id == c.id)
            .order_by(CampaignSend.created_at.asc())
            .limit(100)
        )
    ).all()
    return {
        "campaign": _campaign_public(c),
        "metrics": metrics,
        "sends_sample": [
            {
                "contact_wa_id": wa_id,
                "contact_name": name,
                "status": s.status,
                "wamid": s.wamid,
                "sent_at": s.sent_at.isoformat() if s.sent_at else None,
            }
            for s, wa_id, name in sends
        ],
    }


@router.post("/tenants/{tenant_id}/campaigns/{campaign_id}/estimate")
async def estimate_campaign(
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Destinatarios estimados ANTES de lanzar (segmento + opt-in aplicado).

    Llamar esto antes del launch es el paso que evita sorpresas: muestra
    cuántos contactos reales recibirían el mensaje.
    """
    require_tenant_access(tenant_id, user)
    c = await _get_scoped_campaign(session, campaign_id, user)
    try:
        n = await campaign_svc.estimate_recipients(session, tenant_id, c.segment)
    except campaign_svc.CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"campaign_id": str(c.id), "estimated_recipients": n}


@router.post("/tenants/{tenant_id}/campaigns/{campaign_id}/launch")
async def launch_campaign(
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Lanza la campaña: valida plantilla aprobada por Meta (422 si no) y
    encola los envíos. El envío real con pacing lo hace el worker."""
    require_tenant_access(tenant_id, user)
    try:
        c = await campaign_svc.launch_campaign(
            session, tenant_id, campaign_id, dry_run=False
        )
    except campaign_svc.CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return _campaign_public(c)


@router.post("/tenants/{tenant_id}/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    require_tenant_access(tenant_id, user)
    try:
        c = await campaign_svc.cancel_campaign(session, tenant_id, campaign_id)
    except campaign_svc.CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return _campaign_public(c)


# ── Plantillas: estado de aprobación Meta ───────────────────────────────


@router.get("/tenants/{tenant_id}/templates")
async def list_templates(
    tenant_id: uuid.UUID,
    status: str | None = Query(default=None,
                              description="pending|approved|rejected"),
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Plantillas HSM del tenant (la UI de campañas filtra las aprobadas)."""
    require_tenant_access(tenant_id, user)
    q = select(Template).where(Template.tenant_id == tenant_id).order_by(
        Template.name.asc()
    )
    if status is not None:
        if status not in TEMPLATE_STATUSES:
            raise HTTPException(status_code=422, detail="status inválido")
        q = q.where(Template.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "category": t.category,
            "language": t.language,
            "body": t.body,
            "variables": t.variables,
            "status": t.status,
        }
        for t in rows
    ]


class TemplateStatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in TEMPLATE_STATUSES:
            raise ValueError(
                f"status válido: {' | '.join(TEMPLATE_STATUSES)}"
            )
        return v


@router.patch("/tenants/{tenant_id}/templates/{template_id}/status")
async def set_template_status(
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateStatusUpdate,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Marca el estado de aprobación de Meta de una plantilla.

    Flujo real: el operador crea la plantilla en el Business Manager, Meta
    la aprueba (días de calendario), y AQUÍ se refleja ese estado. El launch
    de campañas solo acepta 'approved'.
    """
    require_tenant_access(tenant_id, user)
    tpl = await session.get(Template, template_id)
    if tpl is None or tpl.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="plantilla no encontrada")
    tpl.status = body.status
    await log_event(
        session, tenant_id, "template.status_changed",
        {"template_id": str(tpl.id), "name": tpl.name,
         "status": body.status, "by": user.email},
    )
    await session.commit()
    return {"id": str(tpl.id), "name": tpl.name, "status": tpl.status}


# ── Contactos: opt-in visible + tags ────────────────────────────────────


@router.get("/tenants/{tenant_id}/contacts")
async def list_contacts(
    tenant_id: uuid.UUID,
    opt_in: bool | None = Query(default=None,
                                description="filtra por marketing_opt_in"),
    contact_type: str | None = Query(default=None,
                                     description="client|prospect"),
    q: str | None = Query(default=None, max_length=60,
                          description="busca en wa_id/nombre"),
    user: CurrentUser = Depends(require_tenant_role(*PANEL_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Lista de contactos con opt-in de marketing visible (Fase 6)."""
    require_tenant_access(tenant_id, user)
    stmt = (
        select(Contact)
        .where(Contact.tenant_id == tenant_id)
        .order_by(Contact.last_interaction_at.desc().nulls_last(),
                  Contact.created_at.desc())
        .limit(200)
    )
    if opt_in is not None:
        stmt = stmt.where(Contact.marketing_opt_in.is_(opt_in))
    if contact_type is not None:
        if contact_type not in ("client", "prospect"):
            raise HTTPException(status_code=422, detail="contact_type inválido")
        stmt = stmt.where(Contact.contact_type == contact_type)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Contact.wa_id.like(like)) | (Contact.name.like(like))
        )
    rows = (await session.execute(stmt)).scalars().all()
    tag_rows: list = []
    if rows:
        tag_rows = (
            await session.execute(
                select(ContactTag.contact_id, ContactTag.tag).where(
                    ContactTag.tenant_id == tenant_id,
                    ContactTag.contact_id.in_([c.id for c in rows]),
                )
            )
        ).all()
    tags_by_contact: dict = {}
    for cid, tag in tag_rows:
        tags_by_contact.setdefault(str(cid), []).append(tag)
    return [
        {
            "id": str(c.id),
            "wa_id": c.wa_id,
            "name": c.name,
            "contact_type": c.contact_type,
            "marketing_opt_in": c.marketing_opt_in,
            "marketing_opt_in_at": (
                c.marketing_opt_in_at.isoformat()
                if c.marketing_opt_in_at else None
            ),
            "marketing_opt_in_source": c.marketing_opt_in_source,
            "tags": tags_by_contact.get(str(c.id), []),
            "last_interaction_at": (
                c.last_interaction_at.isoformat()
                if c.last_interaction_at else None
            ),
        }
        for c in rows
    ]


class OptInBody(BaseModel):
    opt_in: bool
    source: str = "panel"  # panel|import|onboarding (keyword lo pone el drenador)

    @field_validator("source")
    @classmethod
    def _source(cls, v):
        if v not in OPTIN_SOURCES or v == "keyword":
            raise ValueError(
                "source válido: 'panel' | 'import' | 'onboarding'"
            )
        return v


@router.post("/tenants/{tenant_id}/contacts/{contact_id}/opt-in")
async def set_contact_opt_in(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: OptInBody,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Toggle manual de opt-in de marketing (con evidencia de la fuente)."""
    require_tenant_access(tenant_id, user)
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="contacto no encontrado")
    await set_opt_in(session, tenant_id, contact, body.opt_in, body.source)
    await session.commit()
    return {
        "id": str(contact.id),
        "marketing_opt_in": contact.marketing_opt_in,
        "marketing_opt_in_source": contact.marketing_opt_in_source,
    }


class TagBody(BaseModel):
    tag: str = Field(min_length=1, max_length=40)

    @field_validator("tag")
    @classmethod
    def _tag(cls, v):
        return v.strip().lower().replace(" ", "_")


@router.post("/tenants/{tenant_id}/contacts/{contact_id}/tags",
             status_code=201)
async def add_contact_tag(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: TagBody,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    require_tenant_access(tenant_id, user)
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="contacto no encontrado")
    existing = (
        await session.execute(
            select(ContactTag).where(
                ContactTag.tenant_id == tenant_id,
                ContactTag.contact_id == contact.id,
                ContactTag.tag == body.tag,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(ContactTag(
            tenant_id=tenant_id, contact_id=contact.id, tag=body.tag
        ))
        await session.commit()
    return {"contact_id": str(contact.id), "tag": body.tag}


@router.delete("/tenants/{tenant_id}/contacts/{contact_id}/tags/{tag}")
async def remove_contact_tag(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    tag: str,
    user: CurrentUser = Depends(
        require_tenant_role(*CAMPAIGN_ADMIN_ROLES)
    ),
    session: AsyncSession = Depends(get_session),
):
    require_tenant_access(tenant_id, user)
    row = (
        await session.execute(
            select(ContactTag).where(
                ContactTag.tenant_id == tenant_id,
                ContactTag.contact_id == contact_id,
                ContactTag.tag == tag,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="tag no encontrado")
    await session.delete(row)
    await session.commit()
    return {"status": "deleted"}


# ── Métricas (lente "ganar clientes", Fase 5b) ──────────────────────────


def _is_outside_hours(local_dt: datetime, hours: dict) -> bool:
    """True si `local_dt` (hora local del tenant, naive) cae fuera de horario.

    `hours` tiene la forma validada por `_validate_business_hours`:
    {día_en: {open, close: "HH:MM"} | "closed"}. Un día ausente se trata como
    cerrado; un horario malformado cuenta como fuera (postura conservadora).
    """
    day = WEEKDAYS[local_dt.weekday()]
    spec = hours.get(day, "closed")
    if spec == "closed" or not isinstance(spec, dict):
        return True
    try:
        open_h, open_m = (int(x) for x in spec["open"].split(":"))
        close_h, close_m = (int(x) for x in spec["close"].split(":"))
    except Exception:
        return True
    t = (local_dt.hour, local_dt.minute)
    return t < (open_h, open_m) or t >= (close_h, close_m)


async def _after_hours_metrics(
    session: AsyncSession, tenant: Tenant, cutoff: datetime
) -> dict | None:
    """Mensajes inbound fuera de horario que recibieron respuesta (Fase 5b).

    - Cohorte: mensajes `inbound` del tenant en la ventana.
    - Fuera de horario: hora local del tenant (`Tenant.timezone`) fuera de
      `TenantConfig.business_hours`.
    - Atendido: existe un `outbound` del mismo contacto posterior al inbound
      (no distingue bot vs. humano: ambos cuentan como atención).
    - `created_at` se guarda naive (convención: UTC); se interpreta como UTC
      antes de convertir a la zona del tenant.

    Devuelve None si el tenant no tiene horarios configurados o la zona
    horaria es inválida (el panel muestra "—" en vez de un número mentiroso).
    """
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    hours = (cfg.business_hours or {}) if cfg else {}
    if not hours:
        return None
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        return None

    rows = (
        await session.execute(
            select(Message.contact_id, Message.direction, Message.created_at)
            .where(
                Message.tenant_id == tenant.id,
                Message.created_at >= cutoff,
            )
            .order_by(Message.created_at.asc())
        )
    ).all()
    inbound: list[tuple] = []
    outbound_by_contact: dict = {}
    for contact_id, direction, created_at in rows:
        if direction == "inbound":
            inbound.append((contact_id, created_at))
        elif direction == "outbound":
            outbound_by_contact.setdefault(contact_id, []).append(created_at)

    outside = attended = 0
    for contact_id, created_at in inbound:
        local = created_at.replace(tzinfo=timezone.utc).astimezone(tz)
        if not _is_outside_hours(local.replace(tzinfo=None), hours):
            continue
        outside += 1
        if any(o > created_at for o in outbound_by_contact.get(contact_id, [])):
            attended += 1
    return {
        "outside_hours": outside,
        "attended": attended,
        "attended_pct": round(attended / outside * 100, 1) if outside else None,
    }


# ── Métricas ──────────────────────────────────────────────────────────────


@router.get("/tenants/{tenant_id}/metrics")
async def get_tenant_metrics(
    tenant_id: uuid.UUID,
    days: int = Query(default=30, ge=1, le=365),
    user: CurrentUser = Depends(
        require_tenant_role(ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    """Métricas del tenant en los últimos `days` días.

    - `auto_resolution_pct`: conversaciones cerradas SIN handoff / total de
      cerradas × 100 (cohorte = conversaciones abiertas en la ventana).
    - `transfers`: handoffs creados en la ventana.
    - `avg_first_response_seconds`: de messages (primer inbound → primer
      outbound posterior, por conversación).
    - `successful_actions`: action_log con status ok, agrupado por acción.
    - `cost`: de usage_records (agrupado por conversation_id) + agregado
      mensual de usage_monthly.
    - `appointments_scheduled` (Fase 5b): citas creadas en la ventana,
      excluyendo canceladas (las crea el bot vía book_appointment).
    - `leads_captured` (Fase 5b): contactos nuevos en la ventana. El webhook
      crea un Contact al primer mensaje de WhatsApp, así que un contacto
      nuevo = un lead que el asistente capturó. Sin campo `created_by`: se
      cuentan todos los contactos nuevos (nota en DECISIONES_FASE5B.md).
    - `after_hours` (Fase 5b): inbound fuera de horario que recibió
      respuesta; None si el tenant no tiene horarios (ver
      `_after_hours_metrics`).
    """
    require_tenant_access(tenant_id, user)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant no encontrado")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)

    convs = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.opened_at >= cutoff,
            )
            .order_by(Conversation.opened_at.asc())
        )
    ).scalars().all()

    handoffs = (
        await session.execute(
            select(Handoff).where(
                Handoff.tenant_id == tenant_id,
                Handoff.created_at >= cutoff,
            )
        )
    ).scalars().all()

    def _had_handoff(conv: Conversation) -> bool:
        end = conv.closed_at or now
        return any(
            h.contact_id == conv.contact_id
            and conv.opened_at <= h.created_at <= end
            for h in handoffs
        )

    closed = [c for c in convs if c.mode == MODE_RESOLVED]
    auto = [c for c in closed if not _had_handoff(c)]
    auto_pct = round(len(auto) / len(closed) * 100, 1) if closed else None

    # Tiempo a primera respuesta (por conversación).
    first_response_deltas: list[float] = []
    for conv in convs:
        end = conv.closed_at or now
        msgs = (
            await session.execute(
                select(Message.direction, Message.created_at)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.contact_id == conv.contact_id,
                    Message.created_at >= conv.opened_at,
                    Message.created_at <= end,
                )
                .order_by(Message.created_at.asc())
            )
        ).all()
        t_inbound = next(
            (m.created_at for m in msgs if m.direction == "inbound"), None
        )
        t_outbound = next(
            (
                m.created_at
                for m in msgs
                if m.direction == "outbound"
                and t_inbound is not None
                and m.created_at >= t_inbound
            ),
            None,
        )
        if t_inbound and t_outbound:
            first_response_deltas.append(
                (t_outbound - t_inbound).total_seconds()
            )
    avg_first_response = (
        round(sum(first_response_deltas) / len(first_response_deltas), 1)
        if first_response_deltas
        else None
    )

    # Acciones exitosas (action_log).
    action_rows = (
        await session.execute(
            select(ActionLog.action, func.count(ActionLog.id))
            .where(
                ActionLog.tenant_id == tenant_id,
                ActionLog.status == "ok",
                ActionLog.created_at >= cutoff,
            )
            .group_by(ActionLog.action)
        )
    ).all()
    successful_actions = {action: count for action, count in action_rows}

    # Costo (usage_records de la ventana + agregado mensual vigente).
    usage_rows = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageRecord.cost_usd), 0),
                func.coalesce(func.sum(UsageRecord.tokens_in), 0),
                func.coalesce(func.sum(UsageRecord.tokens_out), 0),
                func.count(func.distinct(UsageRecord.conversation_id)),
                func.count(func.distinct(UsageRecord.contact_id)),
            ).where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.created_at >= cutoff,
            )
        )
    ).one()
    total_cost = float(usage_rows[0] or 0)
    convs_with_cost = int(usage_rows[3] or 0) or int(usage_rows[4] or 0)
    monthly = (
        await session.execute(
            select(UsageMonthly).where(
                UsageMonthly.tenant_id == tenant_id,
                UsageMonthly.year == now.year,
                UsageMonthly.month == now.month,
            )
        )
    ).scalar_one_or_none()

    # ── Fase 5b: lente "ganar clientes" ──────────────────────────────────
    # Citas agendadas: creadas en la ventana, sin canceladas.
    appointments_scheduled = (
        await session.execute(
            select(func.count(Appointment.id)).where(
                Appointment.tenant_id == tenant_id,
                Appointment.created_at >= cutoff,
                Appointment.status != "cancelled",
            )
        )
    ).scalar() or 0

    # Leads capturados: contactos nuevos en la ventana.
    leads_captured = (
        await session.execute(
            select(func.count(Contact.id)).where(
                Contact.tenant_id == tenant_id,
                Contact.created_at >= cutoff,
            )
        )
    ).scalar() or 0

    # Mensajes fuera de horario atendidos.
    after_hours = await _after_hours_metrics(session, tenant, cutoff)

    return {
        "tenant_id": str(tenant_id),
        "days": days,
        "conversations": {
            "total": len(convs),
            "closed": len(closed),
            "auto_resolved": len(auto),
            "auto_resolution_pct": auto_pct,
        },
        "transfers": len(handoffs),
        "avg_first_response_seconds": avg_first_response,
        "first_responses_measured": len(first_response_deltas),
        "successful_actions": successful_actions,
        "appointments_scheduled": int(appointments_scheduled),
        "leads_captured": int(leads_captured),
        "after_hours": after_hours,
        "cost_usd": {
            "total_window": round(total_cost, 6),
            "tokens_in": int(usage_rows[1] or 0),
            "tokens_out": int(usage_rows[2] or 0),
            "per_conversation": (
                round(total_cost / convs_with_cost, 6)
                if convs_with_cost
                else None
            ),
            "monthly_total": float(monthly.cost_usd) if monthly else 0.0,
        },
    }
