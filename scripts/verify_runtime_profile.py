from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.config.main import Settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an SSP Licensing Agent runtime profile without printing secrets."
    )
    parser.add_argument(
        "--profile",
        choices=("local_demo", "production"),
        required=True,
    )
    args = parser.parse_args()

    os.environ["RUNTIME_PROFILE"] = args.profile
    settings = Settings()

    if settings.rate_card_backend == "local":
        workbook = Path(settings.rate_card_local_path)
        if not workbook.is_file():
            raise SystemExit(f"Local rate-card workbook was not found: {workbook}")

    evidence = {
        "runtime_profile": settings.effective_runtime_profile,
        "environment": settings.environment,
        "workflow_mode": settings.workflow_mode,
        "rate_card_backend": settings.rate_card_backend,
        "workflow_store_backend": settings.workflow_store_backend,
        "message_dispatch_backend": settings.message_dispatch_backend,
        "ai_intent_backend": settings.ai_intent_backend,
        "requirement_capture_backend": settings.requirement_capture_backend,
        "whatsapp_credentials_present": all(
            (
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                settings.whatsapp_webhook_verify_token,
                settings.whatsapp_app_secret,
            )
        ),
        "whatsapp_access_mode": (
            "public" if settings.whatsapp_allow_all_sellers else "allowlist"
        ),
        "seller_allowlist_count": len(settings.seller_allowlist),
        "openai_key_present": bool(settings.openai_api_key),
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
