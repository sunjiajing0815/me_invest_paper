"""Thin Jinja2 render helper — loads templates from the project-root templates/ directory."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
    undefined=StrictUndefined,
)
# Python string methods not available as Jinja2 filters by default
_env.filters["ljust"] = lambda s, w: str(s).ljust(w)
_env.filters["rjust"] = lambda s, w: str(s).rjust(w)


def render_template(name: str, **ctx: object) -> str:
    """Render the named template with the given context variables."""
    return _env.get_template(name).render(**ctx)
