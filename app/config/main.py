from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_uri: str = "https://graph.facebook.com"
    whatsapp_uri_version: str = "v25.0"
    whatsapp_connect_retries: int = 3
    whatsapp_webhook_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_seller_allowlist: str = ""
    whatsapp_validate_credentials_on_startup: bool = True

    rate_card_backend: Literal["local", "azure_blob"] = "local"
    rate_card_local_path: Path = Path("docs/blob_storage.xlsx")
    rate_card_sheet_name: str = "Outcome Sheet"
    rate_card_storage_account_url: str | None = None
    rate_card_storage_connection_string: str | None = None
    rate_card_container_name: str = "pricing-workbooks"
    rate_card_blob_name: str = "blob_storage.xlsx"

    workflow_store_backend: Literal["memory", "azure_blob"] = "memory"
    workflow_blob_container_name: str = "licensing-workflows"
    workflow_blob_prefix: str = "sessions"

    message_dispatch_backend: Literal["direct", "service_bus"] = "direct"
    service_bus_fully_qualified_namespace: str | None = None
    service_bus_connection_string: str | None = None
    service_bus_queue_name: str = "whatsapp-inbound"

    ai_intent_backend: Literal["disabled", "openai"] = "disabled"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    openai_reasoning_effort: Literal["none", "low", "medium", "high"] = "none"

    default_term_duration: str = "P1Y"
    default_billing_plan: str = "Annual"
    default_customer_segment: str = "Commercial"
    default_currency: str = "INR"
    sku_match_threshold: float = Field(default=90.0, ge=0, le=100)
    migration_seed_path: Path = Path("config/migration_seed.json")
    max_document_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    session_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)

    log_level: str = "INFO"
    port: int = 8000

    @computed_field
    @property
    def seller_allowlist(self) -> frozenset[str]:
        return frozenset(
            value.strip().lstrip("+")
            for value in self.whatsapp_seller_allowlist.split(",")
            if value.strip()
        )

    @model_validator(mode="after")
    def validate_backends(self) -> "Settings":
        if self.rate_card_backend == "azure_blob" and not (
            self.rate_card_storage_connection_string
            or self.rate_card_storage_account_url
        ):
            raise ValueError(
                "Azure Blob rate cards require RATE_CARD_STORAGE_ACCOUNT_URL "
                "or RATE_CARD_STORAGE_CONNECTION_STRING."
            )
        if self.workflow_store_backend == "azure_blob" and not (
            self.rate_card_storage_connection_string
            or self.rate_card_storage_account_url
        ):
            raise ValueError(
                "Azure Blob workflow storage requires RATE_CARD_STORAGE_ACCOUNT_URL "
                "or RATE_CARD_STORAGE_CONNECTION_STRING."
            )
        if self.message_dispatch_backend == "service_bus" and not (
            self.service_bus_connection_string
            or self.service_bus_fully_qualified_namespace
        ):
            raise ValueError(
                "Service Bus dispatch requires SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE "
                "or SERVICE_BUS_CONNECTION_STRING."
            )
        if self.ai_intent_backend == "openai" and not self.openai_api_key:
            raise ValueError("OpenAI intent routing requires OPENAI_API_KEY.")
        if self.environment == "production":
            missing_whatsapp = [
                name
                for name, value in {
                    "WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token,
                    "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
                    "WHATSAPP_WEBHOOK_VERIFY_TOKEN": self.whatsapp_webhook_verify_token,
                    "WHATSAPP_APP_SECRET": self.whatsapp_app_secret,
                }.items()
                if not value
            ]
            if missing_whatsapp:
                raise ValueError(
                    "Production WhatsApp configuration is incomplete: "
                    + ", ".join(missing_whatsapp)
                )
            if not self.whatsapp_validate_credentials_on_startup:
                raise ValueError(
                    "Production requires WHATSAPP_VALIDATE_CREDENTIALS_ON_STARTUP=true."
                )
            if not self.seller_allowlist:
                raise ValueError("Production requires WHATSAPP_SELLER_ALLOWLIST.")
            if self.workflow_store_backend != "azure_blob":
                raise ValueError(
                    "Production requires WORKFLOW_STORE_BACKEND=azure_blob."
                )
            if self.rate_card_backend != "azure_blob":
                raise ValueError("Production requires RATE_CARD_BACKEND=azure_blob.")
            if self.message_dispatch_backend != "service_bus":
                raise ValueError(
                    "Production requires MESSAGE_DISPATCH_BACKEND=service_bus."
                )
            if self.ai_intent_backend != "openai":
                raise ValueError(
                    "Production requires AI_INTENT_BACKEND=openai."
                )
        return self
