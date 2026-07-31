from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_uri: str = "https://graph.facebook.com"
    whatsapp_uri_version: str = "v25.0"
    whatsapp_connect_retries: int = 3
    whatsapp_webhook_verify_token: str
    whatsapp_app_secret: str

    pricing_agent_base_url: str
    pricing_agent_api_key: str
    pricing_agent_timeout_seconds: float = 120.0
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_pricing_agent_configuration(self) -> "Settings":
        if not self.pricing_agent_base_url.startswith(("http://", "https://")):
            raise ValueError("PRICING_AGENT_BASE_URL must start with http:// or https://.")
        if self.pricing_agent_api_key.startswith(("http://", "https://")):
            raise ValueError("PRICING_AGENT_API_KEY must be an API key, not a URL.")
        return self
