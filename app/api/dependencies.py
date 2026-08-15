from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from openai import OpenAIError

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.config import Settings, configure_logging, get_logger
from app.core.dispatch import (
    AzureBlobWebhookDispatcher,
    DirectWebhookDispatcher,
    WebhookDispatcher,
)
from app.core.licensing.agent import (
    IntentInterpreter,
    OpenAIIntentInterpreter,
    OpenAIMicrosoftRecommendationAdvisor,
    RecommendationAdvisor,
)
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.capture import OpenAIRequirementExtractor, RequirementExtractor
from app.core.licensing.migration_rules import MigrationSeedCatalog
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import (
    AzureBlobRateCardSource,
    LocalRateCardSource,
    RateCardProvider,
)
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import (
    AzureBlobWorkflowStore,
    InMemoryWorkflowStore,
    WorkflowStore,
)
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    configure_logging(settings.log_level)
    logger.info(
        "Application startup profile=%s environment=%s rate_card=%s "
        "workflow_store=%s dispatch=%s",
        settings.effective_runtime_profile,
        settings.environment,
        settings.rate_card_backend,
        settings.workflow_store_backend,
        settings.message_dispatch_backend,
    )

    whatsapp_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        transport=httpx.AsyncHTTPTransport(retries=settings.whatsapp_connect_retries),
    )
    whatsapp_client = WhatsAppClient(
        http_client=whatsapp_http_client,
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        base_url=settings.whatsapp_uri,
        api_version=settings.whatsapp_uri_version,
    )
    logger.info(
        "WhatsApp endpoint configured host=%s api_version=%s",
        urlsplit(settings.whatsapp_uri).hostname,
        settings.whatsapp_uri_version,
    )

    if settings.whatsapp_validate_credentials_on_startup:
        try:
            await whatsapp_client.validate_credentials()
        except WhatsAppAPIError:
            logger.exception("WhatsApp credential validation failed")
            if (
                settings.environment == "production"
                or settings.runtime_profile == "local_demo"
            ):
                await whatsapp_http_client.aclose()
                raise

    if settings.rate_card_backend == "azure_blob":
        rate_card_source = AzureBlobRateCardSource(
            container_name=settings.rate_card_container_name,
            blob_name=settings.rate_card_blob_name,
            account_url=settings.rate_card_storage_account_url,
            connection_string=settings.rate_card_storage_connection_string,
        )
    else:
        rate_card_source = LocalRateCardSource(settings.rate_card_local_path)
    rate_cards = RateCardProvider(
        rate_card_source,
        sheet_name=settings.rate_card_sheet_name,
    )

    workflow_store: WorkflowStore
    if settings.workflow_store_backend == "azure_blob":
        blob_store = AzureBlobWorkflowStore(
            container_name=settings.workflow_blob_container_name,
            prefix=settings.workflow_blob_prefix,
            account_url=settings.rate_card_storage_account_url,
            connection_string=settings.rate_card_storage_connection_string,
            session_ttl_hours=settings.session_ttl_hours,
        )
        await blob_store.connect()
        workflow_store = blob_store
    else:
        workflow_store = InMemoryWorkflowStore()

    analyzer = LicenseAnalyzer(
        rate_cards,
        match_threshold=settings.sku_match_threshold,
        default_term_duration=settings.default_term_duration,
        default_billing_plan=settings.default_billing_plan,
    )
    migration_seeds = (
        None
        if settings.workflow_mode == "simple_pricing"
        else MigrationSeedCatalog.load(settings.migration_seed_path)
    )
    scenario_engine = ScenarioEngine(
        migration_seeds,
        apply_bundle_rules=settings.workflow_mode == "scenario_comparison",
        price_basis=(
            "marketplace"
            if settings.workflow_mode == "simple_pricing"
            else "partner_best_offer"
        ),
    )
    catalog = await rate_cards.get()
    if settings.workflow_mode == "simple_pricing" and not any(
        item.marketplace_price > 0 for item in catalog.items
    ):
        raise ValueError(
            "The simple pricing workflow requires a populated current-price column."
        )
    if settings.workflow_mode in {"upgrade_comparison", "scenario_comparison"}:
        scenario_engine.validate_catalog(
            catalog,
            term_duration=settings.default_term_duration,
            billing_plan=settings.default_billing_plan,
            segment=settings.default_customer_segment,
        )
    logger.info(
        "Rate card validated version=%s rows=%s workflow_mode=%s "
        "migration_seed_rules=%s approved=%s",
        catalog.version,
        len(catalog.items),
        settings.workflow_mode,
        len(migration_seeds.rules) if migration_seeds is not None else 0,
        (
            sum(rule.approved for rule in migration_seeds.rules)
            if migration_seeds is not None
            else 0
        ),
    )
    orchestrator = LicensingOrchestrator(
        analyzer=analyzer,
        rate_cards=rate_cards,
        scenarios=scenario_engine,
        store=workflow_store,
        default_term_duration=settings.default_term_duration,
        default_billing_plan=settings.default_billing_plan,
        default_segment=settings.default_customer_segment,
    )
    intent_interpreter: IntentInterpreter | None = None
    if settings.ai_intent_backend == "openai":
        intent_interpreter = OpenAIIntentInterpreter(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort,
            workflow_mode=settings.workflow_mode,
            currency=settings.default_currency,
        )
    requirement_extractor: RequirementExtractor | None = None
    if settings.requirement_capture_backend == "openai":
        requirement_extractor = OpenAIRequirementExtractor(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            transcription_model=settings.openai_transcription_model,
            max_audio_seconds=settings.max_audio_seconds,
            reasoning_effort=settings.openai_reasoning_effort,
        )
    recommendation_advisor: RecommendationAdvisor | None = None
    if settings.official_recommendation_backend == "openai_web":
        recommendation_advisor = OpenAIMicrosoftRecommendationAdvisor(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort,
        )
    if settings.openai_validate_models_on_startup:
        try:
            if intent_interpreter is not None:
                await intent_interpreter.validate_model_access()
            if requirement_extractor is not None:
                await requirement_extractor.validate_model_access()
            if recommendation_advisor is not None:
                await recommendation_advisor.validate_model_access()
        except OpenAIError:
            logger.exception("OpenAI model access validation failed")
            if intent_interpreter is not None:
                await intent_interpreter.close()
            if requirement_extractor is not None:
                await requirement_extractor.close()
            if recommendation_advisor is not None:
                await recommendation_advisor.close()
            await workflow_store.close()
            await rate_cards.close()
            await whatsapp_http_client.aclose()
            raise
        logger.info("OpenAI model access validation completed")
    webhook_service = WhatsAppWebhookService(
        whatsapp_client=whatsapp_client,
        orchestrator=orchestrator,
        configuration=ServiceConfiguration(
            seller_allowlist=settings.seller_allowlist,
            max_document_bytes=settings.max_document_bytes,
            allow_all_sellers=settings.whatsapp_allow_all_sellers,
            max_image_bytes=settings.max_image_bytes,
            max_audio_bytes=settings.max_audio_bytes,
            currency=settings.default_currency,
            workflow_mode=settings.workflow_mode,
        ),
        intent_interpreter=intent_interpreter,
        requirement_extractor=requirement_extractor,
        recommendation_advisor=recommendation_advisor,
    )

    dispatcher: WebhookDispatcher
    if settings.message_dispatch_backend == "azure_blob":
        dispatcher = AzureBlobWebhookDispatcher(
            container_name=settings.workflow_blob_container_name,
            prefix=settings.webhook_blob_prefix,
            account_url=settings.rate_card_storage_account_url,
            connection_string=settings.rate_card_storage_connection_string,
            poll_seconds=settings.webhook_blob_poll_seconds,
            max_delivery_count=settings.webhook_max_delivery_count,
        )
    else:
        dispatcher = DirectWebhookDispatcher()
    await dispatcher.start(webhook_service)

    app.state.settings = settings
    app.state.whatsapp_client = whatsapp_client
    app.state.rate_cards = rate_cards
    app.state.scenario_engine = scenario_engine
    app.state.workflow_store = workflow_store
    app.state.licensing_orchestrator = orchestrator
    app.state.whatsapp_webhook_service = webhook_service
    app.state.webhook_dispatcher = dispatcher

    try:
        logger.info("Application startup completed")
        yield
    finally:
        logger.info("Application shutdown started")
        await dispatcher.close()
        await workflow_store.close()
        await rate_cards.close()
        if intent_interpreter is not None:
            await intent_interpreter.close()
        if requirement_extractor is not None:
            await requirement_extractor.close()
        if recommendation_advisor is not None:
            await recommendation_advisor.close()
        await whatsapp_http_client.aclose()
        logger.info("Application shutdown completed")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_whatsapp_webhook_service(request: Request) -> WhatsAppWebhookService:
    return request.app.state.whatsapp_webhook_service


def get_webhook_dispatcher(request: Request) -> WebhookDispatcher:
    return request.app.state.webhook_dispatcher
