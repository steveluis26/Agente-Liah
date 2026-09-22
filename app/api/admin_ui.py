"""UI mínima del panel (Fase 3): páginas server-rendered con Jinja2 en /admin.

Sin build step ni framework JS: HTML + formularios + fetch contra la API
JSON real (`/api/v1/admin/*`). Autenticación por el mismo JWT que la API,
transportado en la cookie httpOnly `liah_admin_token` (la fija el login);
`fetch(..., {credentials: "include"})` la envía automáticamente.

Páginas:
- GET  /admin/          → redirige a la bandeja (o al login si no hay sesión)
- GET  /admin/login     → formulario de login
- POST /admin/login     → verifica credenciales, fija cookie, redirige
- POST /admin/logout    → limpia cookie, redirige al login
- GET  /admin/handoffs  → bandeja (ver/tomar/resolver/devolver al bot)
- GET  /admin/config    → editor de config por tenant
- GET  /admin/metrics   → tablero de métricas y costo
- GET  /admin/recursos       → gestión de recursos reservables (Fase 7d)
- GET  /admin/configuracion  → vista de solo lectura de la config instalada
                              (Fase 7d: recursos, tipos de servicio,
                              aviso de privacidad, horarios, plantilla origen)
- GET  /admin/contacts      → contactos con opt-in, segmentación
                              prospect|client y consentimiento (Fase 6/7d)
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.admin as admin_api
from app.core.auth import (
    _token_from_request,
    authenticate_platform_user,
    create_access_token,
    get_user_from_token,
)
from app.core.config import get_settings
from app.core.db import get_session

router = APIRouter(prefix="/admin", tags=["admin-ui"])

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates"),
    autoescape=True,
)


def _render(name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(_templates.get_template(name).render(**ctx))


async def _ui_user(request: Request, session: AsyncSession):
    return await get_user_from_token(session, _token_from_request(request))


def _require_ui(user):
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    return None


@router.get("/", include_in_schema=False)
async def admin_root(request: Request, session: AsyncSession = Depends(get_session)):
    user = await _ui_user(request, session)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    return RedirectResponse("/admin/handoffs", status_code=303)


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, session: AsyncSession = Depends(get_session)):
    if await _ui_user(request, session) is not None:
        return RedirectResponse("/admin/handoffs", status_code=303)
    return _render("login.html", error=None)


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate_platform_user(session, email, password)
    if user is None:
        return _render("login.html", error="Credenciales inválidas")
    token = create_access_token(user.id, user.role, user.tenant_id)
    resp = RedirectResponse("/admin/handoffs", status_code=303)
    admin_api._set_auth_cookie(resp, token)
    return resp


@router.post("/logout", include_in_schema=False)
async def logout_submit():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(admin_api.ADMIN_JWT_COOKIE, path="/")
    return resp


def _nav_ctx(user, active: str) -> dict:
    return {
        "user_email": user.email,
        "user_role": user.role,
        "is_platform_admin": user.is_platform_admin,
        "active": active,
        "tenant_id": str(user.tenant_id) if user.tenant_id else None,
    }


@router.get("/handoffs", include_in_schema=False)
async def handoffs_page(
    request: Request,
    status: str | None = None,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    try:
        items = await admin_api.list_handoffs(
            status=status, tenant_id=tenant_id, user=user, session=session
        )
        tenants = await admin_api.list_tenants(user=user, session=session)
    except Exception as e:
        # 403 de scope, etc.: se muestra como error en la página.
        return _render(
            "handoffs.html",
            **_nav_ctx(user, "handoffs"),
            items=[],
            tenants=[],
            error=str(e),
            status_filter=status or "",
            tenant_filter=str(tenant_id) if tenant_id else "",
        )
    return _render(
        "handoffs.html",
        **_nav_ctx(user, "handoffs"),
        items=items,
        tenants=tenants,
        error=None,
        status_filter=status or "",
        tenant_filter=str(tenant_id) if tenant_id else "",
    )


@router.get("/config", include_in_schema=False)
async def config_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    # Tenant por defecto: el del usuario tenant-scoped, o el primero.
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    config = None
    error = None
    if tenant_id is not None:
        try:
            config = await admin_api.get_tenant_config(
                tenant_id=tenant_id, user=user, session=session
            )
        except Exception as e:
            error = str(e)
    return _render(
        "config.html",
        **_nav_ctx(user, "config"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        config=config,
        error=error,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    days: int = 30,
    session: AsyncSession = Depends(get_session),
):
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    metrics = None
    error = None
    if tenant_id is not None:
        try:
            metrics = await admin_api.get_tenant_metrics(
                tenant_id=tenant_id, days=days, user=user, session=session
            )
        except Exception as e:
            error = str(e)
    return _render(
        "metrics.html",
        **_nav_ctx(user, "metrics"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        days=days,
        metrics=metrics,
        error=error,
    )


@router.get("/onboard", include_in_schema=False)
async def onboard_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Página 'Nuevo cliente' (Fase 4): alta desde plantilla de giro.

    Solo platform_admin. La página lista `templates/*.yaml` vía la API JSON
    y crea el cliente con POST /api/v1/admin/tenants/onboard (misma auth por
    cookie). Sin lógica de negocio propia: todo pasa por el endpoint.
    """
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    if not user.is_platform_admin:
        return _render(
            "onboard.html",
            **_nav_ctx(user, "onboard"),
            error="Solo un platform_admin puede dar de alta clientes.",
        )
    return _render("onboard.html", **_nav_ctx(user, "onboard"), error=None)


@router.get("/campaigns", include_in_schema=False)
async def campaigns_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Página de campañas y avisos (Fase 6).

    Sin lógica de negocio propia: lista plantillas aprobadas y campañas vía
    la API JSON; crear/estimar/lanzar/cancelar se hace con fetch contra
    `/api/v1/admin/*` (misma auth por cookie).
    """
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    campaigns = []
    templates_approved = []
    error = None
    if tenant_id is not None:
        try:
            campaigns = await admin_api.list_campaigns(
                tenant_id=tenant_id, user=user, session=session
            )
            templates_approved = await admin_api.list_templates(
                tenant_id=tenant_id, status="approved", user=user, session=session
            )
        except Exception as e:
            error = str(e)
    can_manage = user.is_platform_admin or user.role == "tenant_admin"
    return _render(
        "campaigns.html",
        **_nav_ctx(user, "campaigns"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        campaigns=campaigns,
        templates_approved=templates_approved,
        can_manage=can_manage,
        error=error,
    )


@router.get("/contacts", include_in_schema=False)
async def contacts_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    opt_in: bool | None = None,
    contact_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Página de contactos (Fase 6/7d): opt-in de marketing, segmentación
    prospect|client y consentimiento de privacidad visibles por contacto.
    Todo vía fetch contra la API JSON."""
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    contacts = []
    error = None
    if tenant_id is not None:
        try:
            contacts = await admin_api.list_contacts(
                tenant_id=tenant_id, opt_in=opt_in, contact_type=contact_type,
                q=None, user=user, session=session,
            )
        except Exception as e:
            error = str(e)
    can_manage = user.is_platform_admin or user.role == "tenant_admin"
    return _render(
        "contacts.html",
        **_nav_ctx(user, "contacts"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        contacts=contacts,
        can_manage=can_manage,
        opt_in_filter="" if opt_in is None else ("1" if opt_in else "0"),
        contact_type_filter=contact_type or "",
        error=error,
    )


@router.get("/recursos", include_in_schema=False)
async def resources_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Página de recursos (Fase 7d): listar, crear, editar y eliminar con
    confirmación. El borrado con citas futuras se bloquea en la API (409).
    Sin lógica de negocio propia: todo vía fetch contra la API JSON."""
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    resources = []
    error = None
    if tenant_id is not None:
        try:
            resources = await admin_api.list_resources(
                tenant_id=tenant_id, user=user, session=session
            )
        except Exception as e:
            error = str(e)
    can_manage = user.is_platform_admin or user.role == "tenant_admin"
    return _render(
        "recursos.html",
        **_nav_ctx(user, "recursos"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        resources=resources,
        can_manage=can_manage,
        error=error,
    )


@router.get("/configuracion", include_in_schema=False)
async def configuracion_page(
    request: Request,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Vista de solo lectura de la configuración instalada (Fase 7d): lo que
    puso el levantamiento y el onboarding, la misma fuente que usan el
    chatbot y el CRM. Sin edición aquí."""
    user = await _ui_user(request, session)
    redir = _require_ui(user)
    if redir:
        return redir
    tenants = await admin_api.list_tenants(user=user, session=session)
    if tenant_id is None:
        if user.tenant_id:
            tenant_id = user.tenant_id
        elif tenants:
            tenant_id = uuid.UUID(tenants[0]["id"])
    installed = None
    error = None
    if tenant_id is not None:
        try:
            installed = await admin_api.get_installed_config(
                tenant_id=tenant_id, user=user, session=session
            )
        except Exception as e:
            error = str(e)
    return _render(
        "configuracion.html",
        **_nav_ctx(user, "configuracion"),
        tenants=tenants,
        tenant_filter=str(tenant_id) if tenant_id else "",
        installed=installed,
        error=error,
    )
