"""Configuración central vía pydantic-settings (lee .env)."""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Valores default que JAMÁS deben llegar a producción.
_DEFAULT_SECRETS = {
    "app_secret_key": "change-me",
    "whatsapp_app_secret": "test_app_secret",
    "whatsapp_verify_token": "test_token_123",
    "liah_jwt_secret": "change-me",
}


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
    # Versión de Graph API des-pineada: se configura por env, no en el código.
    whatsapp_graph_version: str = "v21.0"

    # Pepper global para el hash de API keys (Fase 1: salt por clave + pepper).
    # En producción DEBE venir de variable de entorno / secret manager.
    liah_api_key_pepper: str = ""

    # JWT del panel de operadores (Fase 3). Secreto HS256; expiración en minutos.
    liah_jwt_secret: str = "change-me"
    liah_jwt_expire_minutes: int = 480  # 8h: jornada de un operador

    @model_validator(mode="after")
    def _forbid_default_secrets_in_production(self):
        if self.app_env == "production":
            leaked = [
                name
                for name, default in _DEFAULT_SECRETS.items()
                if getattr(self, name, "") in (default, "")
            ]
            if leaked:
                raise ValueError(
                    "En production estos secretos no pueden ser default/vacíos: "
                    + ", ".join(leaked)
                )
            if not self.liah_api_key_pepper:
                raise ValueError(
                    "En production LIAH_API_KEY_PEPPER debe estar configurado"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
