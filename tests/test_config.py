import unittest

from pydantic import ValidationError

from app.config.main import Settings


class StorageModeSettingsTests(unittest.TestCase):
    def test_local_demo_profile_selects_safe_demo_backends(self) -> None:
        settings = Settings(
            _env_file=None,
            runtime_profile="local_demo",
            environment="production",
            storage_mode="azure_blob",
            rate_card_backend="azure_blob",
            workflow_store_backend="azure_blob",
            message_dispatch_backend="azure_blob",
            ai_intent_backend="disabled",
            requirement_capture_backend="disabled",
            openai_validate_models_on_startup=True,
            openai_api_key="not-a-real-secret",
            whatsapp_access_token="not-a-real-secret",
            whatsapp_phone_number_id="123",
            whatsapp_webhook_verify_token="not-a-real-secret",
            whatsapp_app_secret="not-a-real-secret",
            whatsapp_seller_allowlist="919999999999",
        )

        self.assertEqual(settings.effective_runtime_profile, "local_demo")
        self.assertEqual(settings.environment, "development")
        self.assertEqual(settings.rate_card_backend, "local")
        self.assertEqual(settings.workflow_store_backend, "memory")
        self.assertEqual(settings.message_dispatch_backend, "direct")
        self.assertEqual(settings.ai_intent_backend, "openai")
        self.assertEqual(settings.requirement_capture_backend, "openai")
        self.assertEqual(settings.official_recommendation_backend, "openai_web")
        self.assertFalse(settings.openai_validate_models_on_startup)

    def test_local_demo_profile_requires_whatsapp_and_allowlist(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Local demo WhatsApp configuration is incomplete",
        ):
            Settings(
                _env_file=None,
                runtime_profile="local_demo",
                openai_api_key="not-a-real-secret",
            )

    def test_production_profile_selects_durable_backends(self) -> None:
        settings = Settings(
            _env_file=None,
            runtime_profile="production",
            rate_card_storage_account_url="https://storage.example.invalid",
            openai_api_key="not-a-real-secret",
            whatsapp_access_token="not-a-real-secret",
            whatsapp_phone_number_id="123",
            whatsapp_webhook_verify_token="not-a-real-secret",
            whatsapp_app_secret="not-a-real-secret",
            whatsapp_seller_allowlist="919999999999",
        )

        self.assertEqual(settings.effective_runtime_profile, "production")
        self.assertEqual(settings.environment, "production")
        self.assertEqual(settings.rate_card_backend, "azure_blob")
        self.assertEqual(settings.workflow_store_backend, "azure_blob")
        self.assertEqual(settings.message_dispatch_backend, "azure_blob")
        self.assertEqual(settings.official_recommendation_backend, "openai_web")
        self.assertTrue(settings.openai_validate_models_on_startup)

    def test_production_profile_allows_explicit_public_whatsapp_access(self) -> None:
        settings = Settings(
            _env_file=None,
            runtime_profile="production",
            rate_card_storage_account_url="https://storage.example.invalid",
            openai_api_key="not-a-real-secret",
            whatsapp_access_token="not-a-real-secret",
            whatsapp_phone_number_id="123",
            whatsapp_webhook_verify_token="not-a-real-secret",
            whatsapp_app_secret="not-a-real-secret",
            whatsapp_seller_allowlist="",
            whatsapp_allow_all_sellers=True,
        )

        self.assertTrue(settings.whatsapp_allow_all_sellers)
        self.assertEqual(settings.seller_allowlist, frozenset())

    def test_production_rejects_empty_allowlist_without_public_access(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "WHATSAPP_ALLOW_ALL_SELLERS=true",
        ):
            Settings(
                _env_file=None,
                runtime_profile="production",
                rate_card_storage_account_url="https://storage.example.invalid",
                openai_api_key="not-a-real-secret",
                whatsapp_access_token="not-a-real-secret",
                whatsapp_phone_number_id="123",
                whatsapp_webhook_verify_token="not-a-real-secret",
                whatsapp_app_secret="not-a-real-secret",
                whatsapp_seller_allowlist="",
            )

    def test_default_local_workflow_uses_v6_distributor_outcome(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.workflow_mode, "simple_pricing")
        self.assertEqual(settings.simple_price_basis, "distributor_expected")
        self.assertEqual(
            settings.rate_card_local_path.as_posix(),
            "docs/microsoft_sku_v6_distributor.xlsx",
        )
        self.assertEqual(settings.rate_card_sheet_name, "Outcome Sheet")

    def test_local_mode_selects_workbook_and_memory(self) -> None:
        settings = Settings(
            _env_file=None,
            storage_mode="local",
            rate_card_backend="azure_blob",
            workflow_store_backend="azure_blob",
        )

        self.assertEqual(settings.rate_card_backend, "local")
        self.assertEqual(settings.workflow_store_backend, "memory")

    def test_azure_blob_mode_selects_both_blob_backends(self) -> None:
        settings = Settings(
            _env_file=None,
            storage_mode="azure_blob",
            rate_card_backend="local",
            workflow_store_backend="memory",
            rate_card_storage_account_url="https://storage.example.invalid",
        )

        self.assertEqual(settings.rate_card_backend, "azure_blob")
        self.assertEqual(settings.workflow_store_backend, "azure_blob")

    def test_azure_blob_mode_requires_storage_credentials(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Azure Blob rate cards require",
        ):
            Settings(
                _env_file=None,
                storage_mode="azure_blob",
                rate_card_storage_account_url=None,
                rate_card_storage_connection_string=None,
            )

    def test_upgrade_comparison_rejects_nonannual_defaults(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "DEFAULT_TERM_DURATION=P1Y",
        ):
            Settings(
                _env_file=None,
                workflow_mode="upgrade_comparison",
                default_term_duration="P3Y",
                default_billing_plan="Annual",
            )

    def test_production_rejects_storage_connection_string_by_default(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Production connection strings are disabled",
        ):
            Settings(
                _env_file=None,
                environment="production",
                storage_mode="azure_blob",
                rate_card_storage_connection_string="not-a-real-secret",
                message_dispatch_backend="azure_blob",
                ai_intent_backend="openai",
                requirement_capture_backend="openai",
                openai_validate_models_on_startup=True,
                openai_api_key="not-a-real-secret",
                whatsapp_access_token="not-a-real-secret",
                whatsapp_phone_number_id="123",
                whatsapp_webhook_verify_token="not-a-real-secret",
                whatsapp_app_secret="not-a-real-secret",
                whatsapp_seller_allowlist="919999999999",
            )

    def test_production_accepts_managed_identity_resource_urls(self) -> None:
        settings = Settings(
            _env_file=None,
            environment="production",
            storage_mode="azure_blob",
            rate_card_storage_account_url="https://storage.example.invalid",
            message_dispatch_backend="azure_blob",
            ai_intent_backend="openai",
            requirement_capture_backend="openai",
            openai_validate_models_on_startup=True,
            openai_api_key="not-a-real-secret",
            whatsapp_access_token="not-a-real-secret",
            whatsapp_phone_number_id="123",
            whatsapp_webhook_verify_token="not-a-real-secret",
            whatsapp_app_secret="not-a-real-secret",
            whatsapp_seller_allowlist="919999999999",
        )

        self.assertFalse(settings.allow_connection_strings_in_production)

    def test_production_requires_openai_startup_model_validation(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "OPENAI_VALIDATE_MODELS_ON_STARTUP=true",
        ):
            Settings(
                _env_file=None,
                environment="production",
                storage_mode="azure_blob",
                rate_card_storage_account_url="https://storage.example.invalid",
                message_dispatch_backend="azure_blob",
                ai_intent_backend="openai",
                requirement_capture_backend="openai",
                openai_api_key="not-a-real-secret",
                whatsapp_access_token="not-a-real-secret",
                whatsapp_phone_number_id="123",
                whatsapp_webhook_verify_token="not-a-real-secret",
                whatsapp_app_secret="not-a-real-secret",
                whatsapp_seller_allowlist="919999999999",
            )

    def test_production_rejects_non_durable_direct_dispatch(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "MESSAGE_DISPATCH_BACKEND=azure_blob",
        ):
            Settings(
                _env_file=None,
                environment="production",
                storage_mode="azure_blob",
                rate_card_storage_account_url="https://storage.example.invalid",
                message_dispatch_backend="direct",
                ai_intent_backend="openai",
                requirement_capture_backend="openai",
                openai_validate_models_on_startup=True,
                openai_api_key="not-a-real-secret",
                whatsapp_access_token="not-a-real-secret",
                whatsapp_phone_number_id="123",
                whatsapp_webhook_verify_token="not-a-real-secret",
                whatsapp_app_secret="not-a-real-secret",
                whatsapp_seller_allowlist="919999999999",
            )


if __name__ == "__main__":
    unittest.main()
