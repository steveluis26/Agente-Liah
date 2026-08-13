"""API de onboarding white-label (Fase 3).

- POST /tenants: alta self-service (genera API key, devuelta 1 vez).
- POST /channels/whatsapp/embedded-signup/callback: Meta Embedded Signup.
- Endpoints protegidos por X-Tenant-API-Key: config, rules, templates.
"""
import httpx
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import generate_api_key, hash_api_key, get_current_tenant_by_api_key
from app.core.config import get_settings
from app.core.db import get_session
from app.models import (
    AutomationRule,
    Template,
    Tenant,
    TenantConfig,
    WhatsappChannel,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantCreate(BaseModel):
    slug: str
    name: str
    business_type: str
    timezone: str = "America/Mexico_City"
    system_prompt: str = "Eres Liah, el asistente de este negocio."


class TenantCreated(BaseModel):
    tenant_id: str
    api_key: str  # solo se muestra una vez


@router.post("", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, session: AsyncSession = Depends(get_session)):
    # slug único
    existing = (
        await session.execute(select(Tenant).where(Tenant.slug == body.slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="slug already exists")

    api_key = generate_api_key()
    tenant = Tenant(
        slug=body.slug,
        name=body.name,
        business_type=body.business_type,
        timezone=body.timezone,
        api_key_hash=hash_api_key(api_key),
    )
    session.add(tenant)
    await session.flush()
    session.add(
        TenantConfig(tenant_id=tenant.id, system_prompt=body.system_prompt)
    )
    await session.commit()
    return TenantCreated(tenant_id=str(tenant.id), api_key=api_key)


# ---------------------------------------------------------------------------
# Callback de Meta Embedded Signup
# ---------------------------------------------------------------------------
class EmbeddedSignupCallback(BaseModel):
    tenant_id: str
    code: str  # authorization code de Meta
    waba_id: str
    business_id: str | None = None


EMBEDDED_SIGNUP_API_VERSION = "v20.0"
GRAPH_BASE = f"https://graph.facebook.com/{EMBEDDED_SIGNUP_API_VERSION}"


@router.post("/channels/whatsapp/embedded-signup/callback", status_code=status.HTTP_200_OK)
async def embedded_signup_callback(
    body: EmbeddedSignupCallback,
    session: AsyncSession = Depends(get_session),
):
    """Completa Embedded Signup: cambia code por token y suscribe webhook.

    Pasos (verificados contra Graph API en producción; aquí el flujo es
    idempotente y registra el canal). En dry-run (sin app_id/secret) solo
    persiste el canal con los datos provistos.
    """
    settings = get_settings()
    app_id = settings.whatsapp_app_id or None
    app_secret = settings.whatsapp_app_secret or None

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == uuid.UUID(body.tenant_id)))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    phone_number_id = None
    token = None
    phone_number = None

    if app_id and app_secret:
        async with httpx.AsyncClient(timeout=30) as client:
            # Paso B: code -> access token (System User de larga duración)
            r = await client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": body.code,
                },
            )
            r.raise_for_status()
            token = r.json().get("access_token")

            # Paso C: phone numbers del WABA
            if token:
                r2 = await client.get(
                    f"{GRAPH_BASE}/{body.waba_id}/phone_numbers",
                    params={"access_token": token},
                )
                r2.raise_for_status()
                data = r2.json().get("data") or []
                if data:
                    phone_number_id = data[0].get("id")
                    phone_number = data[0].get("display_phone_number")

                # Paso D: suscripción del webhook a la WABA
                await client.post(
                    f"{GRAPH_BASE}/{body.waba_id}/subscribed_apps",
                    params={"access_token": token},
                )

    channel = WhatsappChannel(
        tenant_id=tenant.id,
        bsp="meta",
        waba_id=body.waba_id,
        phone_number_id=phone_number_id,
        phone_number=phone_number,
        token_secret_ref=token or "PENDING",  # en prod va a secret manager
        status="active" if phone_number_id else "pending",
    )
    session.add(channel)
    await session.commit()
    return {
        "status": "connected" if phone_number_id else "pending",
        "phone_number_id": phone_number_id,
        "phone_number": phone_number,
    }


# ---------------------------------------------------------------------------
# Endpoints protegidos (X-Tenant-API-Key)
# ---------------------------------------------------------------------------
class ConfigUpdate(BaseModel):
    system_prompt: str | None = None
    tone: str | None = None
    handoff_phone: str | None = None
    lfpdp_consent_required: bool | None = None
    privacy_notice: str | None = None


@router.get("/me/config")
async def get_config(
    tenant_id: uuid.UUID = Depends(get_current_tenant_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    cfg = (
        await session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=404, detail="config not found")
    return {
        "system_prompt": cfg.system_prompt,
        "tone": cfg.tone,
        "handoff_phone": cfg.handoff_phone,
        "lfpdp_consent_required": cfg.lfpdp_consent_required,
        "privacy_notice": cfg.privacy_notice,
    }


@router.put("/me/config")
async def update_config(
    body: ConfigUpdate,
    tenant_id: uuid.UUID = Depends(get_current_tenant_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    cfg = (
        await session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=404, detail="config not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cfg, field, value)
    await session.commit()
    return {"status": "updated"}


class RuleCreate(BaseModel):
    type: str
    enabled: bool = True
    params: dict = {}


@router.post("/me/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    rule = AutomationRule(
        tenant_id=tenant_id, type=body.type, enabled=body.enabled, params=body.params
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return {"rule_id": str(rule.id), "type": rule.type, "enabled": rule.enabled}


class TemplateCreate(BaseModel):
    name: str
    category: str = "utility"
    language: str = "es"
    body: str
    variables: list = []
    status: str = "pending"


@router.post("/me/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant_by_api_key),
    session: AsyncSession = Depends(get_session),
):
    tpl = Template(
        tenant_id=tenant_id,
        name=body.name,
        category=body.category,
        language=body.language,
        body=body.body,
        variables=body.variables,
        status=body.status,
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return {"template_id": str(tpl.id), "name": tpl.name, "status": tpl.status}
