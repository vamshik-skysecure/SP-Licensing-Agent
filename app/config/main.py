from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional high-level profile for safe, one-setting runtime selection. When it is
    # omitted, the existing individual settings remain authoritative for backward
    # compatibility with deployed environments.
    runtime_profile: Literal["local_demo", "production"] | None = None
    environment: Literal["development", "test", "production"] = "development"

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_uri: str = "https://graph.facebook.com"
    whatsapp_uri_version: str = "v25.0"
    whatsapp_connect_retries: int = 3
    whatsapp_webhook_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_seller_allowlist: str = ""
    whatsapp_allow_all_sellers: bool = False
    whatsapp_validate_credentials_on_startup: bool = True

    # Preferred high-level switch. When omitted, the individual backend
    # settings below remain authoritative for backward compatibility.
    storage_mode: Literal["local", "azure_blob"] | None = None
    workflow_mode: Literal[
        "simple_pricing",
        "renewal_only",
        "upgrade_comparison",
        "scenario_comparison",
    ] = "simple_pricing"
    simple_price_basis: Literal["marketplace", "distributor_expected"] = (
        "distributor_expected"
    )
    rate_card_backend: Literal["local", "azure_blob"] = "local"
    rate_card_local_path: Path = Path("docs/microsoft_sku_v6_distributor.xlsx")
    rate_card_sheet_name: str = "Outcome Sheet"
    rate_card_storage_account_url: str | None = None
    rate_card_storage_connection_string: str | None = None
    rate_card_container_name: str = "pricing-workbooks"
    rate_card_blob_name: str = "active/Microsoft_SKU_V6.0_Distributor.xlsx"

    workflow_store_backend: Literal["memory", "azure_blob"] = "memory"
    workflow_blob_container_name: str = "licensing-workflows"
    workflow_blob_prefix: str = "sessions"

    message_dispatch_backend: Literal["direct", "azure_blob"] = "direct"
    webhook_blob_prefix: str = "webhook-queue"
    webhook_blob_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    webhook_max_delivery_count: int = Field(default=5, ge=1, le=100)
    max_webhook_bytes: int = Field(default=1024 * 1024, gt=0, le=5 * 1024 * 1024)
    allow_connection_strings_in_production: bool = False

    ai_intent_backend: Literal["disabled", "openai"] = "disabled"
    requirement_capture_backend: Literal["disabled", "openai"] = "disabled"
    official_recommendation_backend: Literal["disabled", "openai_web"] = "disabled"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    openai_transcription_model: str = "gpt-transcribe"
    openai_reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    openai_validate_models_on_startup: bool = False

    default_term_duration: str = "P1Y"
    default_billing_plan: str = "Annual"
    default_customer_segment: str = "Commercial"
    default_currency: str = "INR"
    sku_match_threshold: float = Field(default=90.0, ge=0, le=100)
    migration_seed_path: Path = Path("config/migration_seed.json")
    max_document_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_audio_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=25 * 1024 * 1024)
    max_audio_seconds: int = Field(default=300, ge=1, le=30 * 60)
    session_ttl_minutes: int = Field(default=5, ge=1, le=24 * 60)

    log_level: str = "INFO"
    port: int = 8000

    @computed_field
    @property
    def seller_allowlist(self) -> frozenset[str]:
        return frozenset(
            re.sub(r"\D", "", value)
            for value in self.whatsapp_seller_allowlist.split(",")
            if value.strip()
        )

    @computed_field
    @property
    def effective_runtime_profile(self) -> str:
        if self.runtime_profile is not None:
            return self.runtime_profile
        return "production" if self.environment == "production" else "custom"

    @model_validator(mode="after")
    def validate_backends(self) -> "Settings":
        invalid_seller_numbers = []
        for supplied_number in self.whatsapp_seller_allowlist.split(","):
            supplied_number = supplied_number.strip()
            if not supplied_number:
                continue
            normalized_number = re.sub(r"\D", "", supplied_number)
            if (
                re.fullmatch(r"[+\d\s()\-]+", supplied_number) is None
                or re.fullmatch(r"[1-9]\d{7,14}", normalized_number) is None
            ):
                invalid_seller_numbers.append(supplied_number)
        if invalid_seller_numbers:
            raise ValueError(
                "WHATSAPP_SELLER_ALLOWLIST entries must be international phone numbers "
                "including country code."
            )

        if self.runtime_profile == "local_demo":
            self.environment = "development"
            self.storage_mode = "local"
            self.message_dispatch_backend = "direct"
            self.ai_intent_backend = "openai"
            self.requirement_capture_backend = "openai"
            self.official_recommendation_backend = "openai_web"
            self.openai_validate_models_on_startup = False
            self.whatsapp_validate_credentials_on_startup = True
        elif self.runtime_profile == "production":
            self.environment = "production"
            self.storage_mode = "azure_blob"
            self.message_dispatch_backend = "azure_blob"
            self.ai_intent_backend = "openai"
            self.requirement_capture_backend = "openai"
            self.official_recommendation_backend = "openai_web"
            self.openai_validate_models_on_startup = True
            self.whatsapp_validate_credentials_on_startup = True

        if self.storage_mode == "local":
            self.rate_card_backend = "local"
            self.workflow_store_backend = "memory"
        elif self.storage_mode == "azure_blob":
            self.rate_card_backend = "azure_blob"
            self.workflow_store_backend = "azure_blob"

        if self.workflow_mode == "upgrade_comparison" and (
            self.default_term_duration.casefold() != "p1y"
            or self.default_billing_plan.casefold() != "annual"
        ):
            raise ValueError(
                "Upgrade comparison requires DEFAULT_TERM_DURATION=P1Y and "
                "DEFAULT_BILLING_PLAN=Annual."
            )

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
        if self.ai_intent_backend == "openai" and not self.openai_api_key:
            raise ValueError("OpenAI intent routing requires OPENAI_API_KEY.")
        if self.requirement_capture_backend == "openai" and not self.openai_api_key:
            raise ValueError("OpenAI multimodal requirement capture requires OPENAI_API_KEY.")
        if (
            self.official_recommendation_backend == "openai_web"
            and not self.openai_api_key
        ):
            raise ValueError(
                "Official Microsoft recommendation research requires OPENAI_API_KEY."
            )
        if self.runtime_profile == "local_demo":
            missing_demo_settings = [
                name
                for name, value in {
                    "WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token,
                    "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
                    "WHATSAPP_WEBHOOK_VERIFY_TOKEN": self.whatsapp_webhook_verify_token,
                    "WHATSAPP_APP_SECRET": self.whatsapp_app_secret,
                }.items()
                if not value
            ]
            if not self.whatsapp_allow_all_sellers and not self.seller_allowlist:
                missing_demo_settings.append("WHATSAPP_SELLER_ALLOWLIST")
            if missing_demo_settings:
                raise ValueError(
                    "Local demo WhatsApp configuration is incomplete: "
                    + ", ".join(missing_demo_settings)
                )
        if self.environment == "production":
            whatsapp_endpoint = urlsplit(self.whatsapp_uri)
            if (
                whatsapp_endpoint.scheme.casefold() != "https"
                or (whatsapp_endpoint.hostname or "").casefold()
                != "graph.facebook.com"
                or whatsapp_endpoint.port not in {None, 443}
                or whatsapp_endpoint.path not in {"", "/"}
                or bool(whatsapp_endpoint.query or whatsapp_endpoint.fragment)
                or bool(whatsapp_endpoint.username or whatsapp_endpoint.password)
            ):
                raise ValueError(
                    "Production WHATSAPP_URI must be https://graph.facebook.com."
                )
            if self.rate_card_storage_account_url:
                storage_endpoint = urlsplit(self.rate_card_storage_account_url)
                storage_host = (storage_endpoint.hostname or "").casefold()
                if (
                    storage_endpoint.scheme.casefold() != "https"
                    or not storage_host.endswith(".blob.core.windows.net")
                    or storage_endpoint.port not in {None, 443}
                    or storage_endpoint.path not in {"", "/"}
                    or bool(storage_endpoint.query or storage_endpoint.fragment)
                    or bool(storage_endpoint.username or storage_endpoint.password)
                ):
                    raise ValueError(
                        "Production RATE_CARD_STORAGE_ACCOUNT_URL must be an HTTPS "
                        "Azure Blob service endpoint."
                    )
            if not self.allow_connection_strings_in_production and (
                self.rate_card_storage_connection_string
            ):
                raise ValueError(
                    "Production connection strings are disabled. Use the App Service "
                    "managed identity with resource URLs instead."
                )
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
            if not self.whatsapp_allow_all_sellers and not self.seller_allowlist:
                raise ValueError(
                    "Production requires WHATSAPP_SELLER_ALLOWLIST unless "
                    "WHATSAPP_ALLOW_ALL_SELLERS=true is explicitly configured."
                )
            if self.workflow_store_backend != "azure_blob":
                raise ValueError(
                    "Production requires WORKFLOW_STORE_BACKEND=azure_blob."
                )
            if self.rate_card_backend != "azure_blob":
                raise ValueError("Production requires RATE_CARD_BACKEND=azure_blob.")
            if self.message_dispatch_backend != "azure_blob":
                raise ValueError(
                    "Production requires MESSAGE_DISPATCH_BACKEND=azure_blob."
                )
            if self.ai_intent_backend != "openai":
                raise ValueError(
                    "Production requires AI_INTENT_BACKEND=openai."
                )
            if self.requirement_capture_backend != "openai":
                raise ValueError(
                    "Production requires REQUIREMENT_CAPTURE_BACKEND=openai."
                )
            if not self.openai_validate_models_on_startup:
                raise ValueError(
                    "Production requires OPENAI_VALIDATE_MODELS_ON_STARTUP=true."
                )
        return self
