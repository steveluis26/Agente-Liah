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
    Contact,
    Conversation,
    Handoff,
    Message,
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
