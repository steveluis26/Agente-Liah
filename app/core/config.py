"""Configuración central vía pydantic-settings (lee .env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "change-me"

    database_url: str = "postgresql+asyncpg://pyme:pyme@localhost:5432/pyme_agent"
    redis_url: str = "redis://localhost:6379/0"

    whatsapp_app_id: str = ""
    whatsapp_app_secret: str = "test_app_secret"
    whatsapp_verify_token: str = "test_token_123"
    whatsapp_verify_signature: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
