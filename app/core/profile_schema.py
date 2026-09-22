"""Schema del perfil declarativo de giro (Fase 4, extendido en Fase 7b).

Un "perfil" es el YAML versionado en `templates/<giro>.yaml`: todo lo que
define a un cliente (giro, tono, horarios, herramientas, reglas, plantillas
HSM, conocimiento semilla, políticas) SIN tocar código.

Fase 7b (schema 1.1) agrega tres secciones declarativas:
- `resources`: recursos reservables del negocio (salas, especialistas,
  equipo, personal), con movilidad fija o móvil.
- `service_types`: tipos de servicio con duración, recursos requeridos,
  buffers (setup/teardown) y política de traslado (clave para negocios
  móviles que atienden eventos en distintas sedes).
- `privacy_terms`: términos de privacidad que el contacto acepta desde el
  primer mensaje (se guardan con versión y fecha).

Validación estricta (`extra="forbid"` en todos los modelos): una clave
desconocida es error, no se guarda en silencio. `load_template()` lee y
valida un YAML; `list_templates()` enumera los giros disponibles para el
panel/CLI.
"""
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.1"

# Raíz del repo = dos niveles arriba de app/core/.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _tool_names() -> list[str]:
    """Nombres de herramientas reales del motor (app/agent/tools.py)."""
    from app.agent.tools import build_tools

    return [t["function"]["name"] for t in build_tools()]


class _Strict(BaseModel):
    model_config = {"extra": "forbid"}


class ConocimientoItem(_Strict):
    """Un documento semilla de la base de conocimiento del negocio."""

    titulo: str = Field(min_length=3, max_length=200)
    contenido: str = Field(min_length=10)


class PlantillaHSM(_Strict):
    """Plantilla de mensaje (WhatsApp HSM) que el tenant registrará en Meta."""

    nombre: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1)
    variables: list[str] = Field(default_factory=list)
    categoria: Literal["utility", "marketing", "authentication"] = "utility"
    idioma: str = Field(default="es", min_length=2, max_length=10)


class ReglaAutomatizacion(_Strict):
    """Regla de automatización/recordatorio (se persiste en automation_rules).

    El `tipo` es libre (la semántica la implementa el scheduler/dispatch);
    tipos conocidos hoy: `appointment_reminder`, `followup_30d`, `custom`.
    Los legacy `trial_class`/`colegiatura` siguen aceptados por compatibilidad
    con tenants creados en fases anteriores.
    """

    tipo: str = Field(min_length=1, max_length=40)
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class Politicas(_Strict):
    """Políticas de operación del asistente."""

    escalamiento: str = Field(
        default="", max_length=2000,
        description="Cuándo y cómo escalar a un humano.",
    )
    temas_sensibles: list[str] = Field(
        default_factory=list,
        description="Temas que fuerzan handoff inmediato (no los resuelve el bot).",
    )
    consentimiento_recordatorios: bool = True


# ── Fase 7b: recursos, tipos de servicio y términos de privacidad ────────

TIPO_RECURSO = Literal["room", "specialist", "equipment", "staff"]
MODO_TRASLADO = Literal["none", "fixed", "per_zone"]


class Recurso(_Strict):
    """Un recurso reservable del negocio (sala, especialista, equipo,
    personal). Los fijos viven en una sede; los móviles se desplazan
    (negocios de eventos: el recurso viaja con el servicio)."""

    slug: str = Field(min_length=2, max_length=60)
    nombre: str = Field(min_length=1, max_length=120)
    tipo: TIPO_RECURSO
    movilidad: Literal["fixed", "mobile"] = "fixed"
    capacidad: int = Field(default=1, ge=1)
    especialidad: str | None = Field(default=None, max_length=60)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug de recurso inválido: solo minúsculas, dígitos y "
                "guiones, sin guion al inicio/fin"
            )
        return v


class BuffersMin(_Strict):
    """Minutos de preparación/limpieza alrededor del servicio."""

    setup: int = Field(default=0, ge=0)
    teardown: int = Field(default=0, ge=0)


class Traslado(_Strict):
    """Política de traslado para negocios móviles.

    - none: sin traslado (negocio fijo o servicio en sede del cliente sin
      costo de tiempo).
    - fixed: minutos fijos de traslado por servicio (`fixed_min` requerido).
    - per_zone: minutos por zona (`zonas` no vacío); `default_min` aplica a
      zonas no listadas.
    """

    modo: MODO_TRASLADO = "none"
    fixed_min: int | None = Field(default=None, gt=0)
    default_min: int = Field(default=60, ge=0)
    zonas: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _modo_coherente(self):
        if self.modo == "fixed" and self.fixed_min is None:
            raise ValueError(
                "traslado.fixed_min es requerido cuando modo='fixed'"
            )
        if self.modo == "per_zone":
            if not self.zonas:
                raise ValueError(
                    "traslado.zonas no puede estar vacío cuando "
                    "modo='per_zone'"
                )
            for zona, mins in self.zonas.items():
                if mins < 0:
                    raise ValueError(
                        f"traslado.zonas[{zona!r}]: los minutos no pueden "
                        "ser negativos"
                    )
        return self


class RecursoRequerido(_Strict):
    """Un requerimiento de recurso dentro de un tipo de servicio.

    Discriminación excluyente: o bien se referencia UN recurso concreto por
    slug ({"recurso": "<slug>"}) o bien se pide una CANTIDAD de recursos de
    un tipo ({"tipo": <tipo>, "cantidad": N, "especialidad": ...}). Nunca
    ambas, nunca ninguna.
    """

    recurso: str | None = None
    tipo: TIPO_RECURSO | None = None
    cantidad: int = Field(default=1, ge=1)
    especialidad: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _discriminar(self):
        por_slug = self.recurso is not None
        por_tipo = self.tipo is not None
        if por_slug == por_tipo:
            raise ValueError(
                "recurso requerido inválido: usa exactamente UNA de "
                "{'recurso': slug} o {'tipo': tipo, ...} (son excluyentes)"
            )
        if por_slug and not _SLUG_RE.match(self.recurso):  # type: ignore[arg-type]
            raise ValueError(
                f"slug de recurso referenciado inválido: {self.recurso!r}"
            )
        return self


class TipoServicio(_Strict):
    """Un servicio que el negocio ofrece/agenda: duración, qué recursos
    necesita, buffers y política de traslado."""

    slug: str = Field(min_length=2, max_length=60)
    nombre: str = Field(min_length=1, max_length=120)
    duracion_min: int = Field(gt=0)
    recursos: list[RecursoRequerido] = Field(min_length=1)
    buffers_min: BuffersMin = Field(default_factory=BuffersMin)
    traslado: Traslado = Field(default_factory=Traslado)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug de tipo de servicio inválido: solo minúsculas, "
                "dígitos y guiones, sin guion al inicio/fin"
            )
        return v


class PrivacyTerms(_Strict):
    """Términos de privacidad del tenant: el contacto los acepta desde el
    primer mensaje y se guardan con versión y fecha (Fase 7b)."""

    version: str = Field(min_length=1, max_length=20)
    titulo: str = Field(min_length=3, max_length=160)
    texto: str = Field(min_length=20)


class PerfilGiro(_Strict):
    """Perfil declarativo completo de un giro de negocio."""

    schema_version: Literal["1.0", "1.1"] = "1.1"
    giro: str = Field(min_length=1, max_length=40)
    # slug sugerido al dar de alta (el onboarding puede sobreescribirlo).
    slug: str = Field(min_length=2, max_length=64)
    nombre: str = Field(min_length=1, max_length=160)
    timezone: str = "America/Mexico_City"
    locale: str = Field(default="es-MX", min_length=2, max_length=10)
    system_prompt: str = Field(min_length=20)
    tono: str = Field(min_length=1, max_length=40)
    horarios: dict[str, Any] = Field(default_factory=dict)
    herramientas_habilitadas: list[str] = Field(min_length=1)
    reglas: list[ReglaAutomatizacion] = Field(default_factory=list)
    plantillas_hsm: list[PlantillaHSM] = Field(default_factory=list)
    conocimiento_semilla: list[ConocimientoItem] = Field(min_length=1)
    politicas: Politicas = Field(default_factory=Politicas)
    # Fase 7b (opcionales: los YAML 1.0 sin estas secciones siguen validando).
    resources: list[Recurso] = Field(default_factory=list)
    service_types: list[TipoServicio] = Field(default_factory=list)
    privacy_terms: PrivacyTerms | None = None
    # Override opcional del ruteo comercial default (openai). Se valida con
    # las mismas reglas que PUT /tenants/{id}/config (cero secretos aquí).
    model_routing: dict[str, Any] | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug inválido: solo minúsculas, dígitos y guiones, "
                "sin guion al inicio/fin"
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"timezone inválida: {v!r} (usa formato IANA)")
        return v

    @field_validator("horarios")
    @classmethod
    def _horarios(cls, v: dict) -> dict:
        # Misma forma que PUT /config: {día: {open, close: HH:MM} | "closed"}.
        from app.api.admin import _validate_business_hours

        return _validate_business_hours(v)

    @field_validator("herramientas_habilitadas")
    @classmethod
    def _herramientas(cls, v: list[str]) -> list[str]:
        validas = _tool_names()
        desconocidas = [t for t in v if t not in validas]
        if desconocidas:
            raise ValueError(
                f"herramientas desconocidas: {desconocidas}. "
                f"Válidas: {validas}"
            )
        return v

    @field_validator("model_routing")
    @classmethod
    def _routing(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        from app.api.admin import ModelRoutingUpdate

        # Reusa la validación estricta del panel (extra="forbid", sin secretos).
        return ModelRoutingUpdate(**v).model_dump(exclude_none=True)

    @model_validator(mode="after")
    def _plantillas_unicas(self):
        nombres = [p.nombre for p in self.plantillas_hsm]
        dup = {n for n in nombres if nombres.count(n) > 1}
        if dup:
            raise ValueError(f"plantillas_hsm con nombre duplicado: {sorted(dup)}")
        return self

    @model_validator(mode="after")
    def _recursos_y_servicios_coherentes(self):
        # Slugs únicos dentro de cada colección.
        for coleccion, etiqueta in (
            (self.resources, "resources"),
            (self.service_types, "service_types"),
        ):
            slugs = [r.slug for r in coleccion]
            dup = {s for s in slugs if slugs.count(s) > 1}
            if dup:
                raise ValueError(
                    f"{etiqueta} con slug duplicado: {sorted(dup)}"
                )
        # Cada {"recurso": slug} debe existir en resources.
        conocidos = {r.slug for r in self.resources}
        for st in self.service_types:
            for req in st.recursos:
                if req.recurso is not None and req.recurso not in conocidos:
                    raise ValueError(
                        f"service_type '{st.slug}': referencia un recurso "
                        f"inexistente '{req.recurso}' (resources: "
                        f"{sorted(conocidos) or 'vacío'})"
                    )
        return self


def load_template(path: str | Path) -> PerfilGiro:
    """Lee un YAML de `templates/` y lo valida contra el schema.

    Lanza ValueError con mensaje claro si el YAML es inválido o no cumple
    el schema (la API lo convierte en 422).
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"plantilla no encontrada: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"YAML inválido en {path.name}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: el documento debe ser un objeto YAML")
    try:
        return PerfilGiro(**data)
    except Exception as e:
        raise ValueError(f"{path.name}: perfil inválido: {e}")


def list_templates() -> list[dict]:
    """Enumera los giros disponibles en `templates/*.yaml` (validados).

    Devuelve resúmenes para el panel/CLI. Un YAML inválido se reporta como
    entrada con `error` en vez de tumbar el listado (el operador lo ve y lo
    corrige).
    """
    out = []
    if not TEMPLATES_DIR.is_dir():
        return out
    for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
        try:
            perfil = load_template(path)
            out.append({
                "template": path.stem,
                "giro": perfil.giro,
                "nombre": perfil.nombre,
                "slug_default": perfil.slug,
                "schema_version": perfil.schema_version,
                "herramientas": perfil.herramientas_habilitadas,
                "reglas": len(perfil.reglas),
                "plantillas": len(perfil.plantillas_hsm),
                "conocimiento_items": len(perfil.conocimiento_semilla),
                "recursos": len(perfil.resources),
                "tipos_servicio": len(perfil.service_types),
                "privacy_terms": perfil.privacy_terms is not None,
            })
        except ValueError as e:
            out.append({"template": path.stem, "error": str(e)})
    return out


def apply_overrides(data: dict, overrides: dict | None) -> dict:
    """Aplica overrides del operador sobre el perfil (merge profundo).

    Solo se permiten claves que ya existen en el perfil (una clave nueva es
    error: evita typos que se ignoren en silencio). Los dicts se fusionan
    recursivamente; cualquier otro valor se reemplaza.
    """
    data = dict(data)
    for key, value in (overrides or {}).items():
        if key not in data:
            raise ValueError(
                f"override inválido: {key!r} no existe en el perfil "
                f"(claves válidas: {sorted(data)})"
            )
        current = data[key]
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return data
