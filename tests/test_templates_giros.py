"""Tests de Fase 5b: plantillas de giro (solo YAML + schema, sin código genérico).

Valida que las 4 plantillas en `templates/*.yaml`:
- cargan con `load_template()` (schema estricto, `extra="forbid"`),
- traen conocimiento semilla no vacío,
- solo usan herramientas que existen en `build_tools()`.

No requieren BD: es validación pura de YAML/pydantic.
"""
from app.agent.tools import build_tools
from app.core.profile_schema import list_templates, load_template

TEMPLATES = (
    "consultorio_medico",
    "estetica",
    "escuela_privada",
    "academia_danza",
)

REAL_TOOLS = {t["function"]["name"] for t in build_tools()}


def test_all_templates_load_and_validate():
    for name in TEMPLATES:
        perfil = load_template(f"templates/{name}.yaml")
        assert perfil.schema_version in ("1.0", "1.1")
        assert perfil.giro == name
        assert perfil.slug
        assert len(perfil.system_prompt) >= 20


def test_templates_have_nonempty_seed_knowledge():
    for name in TEMPLATES:
        perfil = load_template(f"templates/{name}.yaml")
        assert perfil.conocimiento_semilla, name
        for item in perfil.conocimiento_semilla:
            assert item.titulo.strip()
            assert item.contenido.strip()


def test_templates_only_use_real_tools():
    for name in TEMPLATES:
        perfil = load_template(f"templates/{name}.yaml")
        unknown = set(perfil.herramientas_habilitadas) - REAL_TOOLS
        assert not unknown, f"{name}: herramientas inexistentes: {unknown}"


def test_list_templates_reports_all_four_without_errors():
    rows = {r["template"]: r for r in list_templates()}
    for name in TEMPLATES:
        assert name in rows, f"falta en list_templates(): {name}"
        assert "error" not in rows[name], rows[name].get("error")
        assert rows[name]["giro"] == name
        assert rows[name]["conocimiento_items"] > 0


def test_new_templates_declare_sensitive_handoff_topics():
    # Las plantillas de fase 5b deben forzar handoff en temas sensibles.
    for name in ("escuela_privada", "academia_danza"):
        perfil = load_template(f"templates/{name}.yaml")
        assert perfil.politicas.temas_sensibles, name
        assert "escalate_to_human" in perfil.herramientas_habilitadas
