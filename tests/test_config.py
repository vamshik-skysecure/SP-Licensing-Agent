import unittest

from pydantic import ValidationError

from app.config.main import Settings


class StorageModeSettingsTests(unittest.TestCase):
    def test_default_local_workflow_uses_v5_final_output(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.workflow_mode, "upgrade_comparison")
        self.assertEqual(
            settings.rate_card_local_path.as_posix(),
            "docs/microsoft_sku_v5.xlsx",
        )
        self.assertEqual(settings.rate_card_sheet_name, "Final Output Sheet")

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


if __name__ == "__main__":
    unittest.main()
