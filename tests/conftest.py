"""conftest raíz: fuerza la BD de test y reconfigura el engine de la app UNA vez.

Debe cargarse antes que cualquier test (pytest lo descubre automáticamente).
"""
import os

os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pyme:pyme@127.0.0.1:5433/pyme_agent_test",
)
# La app lee DATABASE_URL vía pydantic-settings; lo forzamos para que el engine
# de producción apunte a la BD de test.
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import db as db_mod
from app.core.config import get_settings


def pytest_configure(config):
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(url, echo=False, poolclass=NullPool)
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
