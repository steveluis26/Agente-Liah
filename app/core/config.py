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

    # Fase 6 — campañas y avisos.
    # Pacing anti-baneo: mensajes/segundo por defecto (conservador; el
    # operador lo sube por tenant en extra["campaign_msgs_per_sec"]).
    liah_campaign_rate_per_sec: float = 1.0
    # Costo estimado por conversación de marketing (USD). Placeholder
    # calibrable con la matriz de precios de Meta; por tenant en
    # extra["campaign_cost_usd"].
    liah_campaign_cost_usd: float = 0.06

    # Fase 8 — empaque/soporte.
    # CORS: orígenes permitidos separados por coma ("" = sin CORS, solo mismo origen).
    liah_cors_origins: str = ""
    # Rate limiting anti-abuso (middleware en memoria por proceso).
    liah_rate_limit_enabled: bool = True
    # Logging JSON estructurado con tenant_id (ideal en producción).
    liah_log_json: bool = False

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
