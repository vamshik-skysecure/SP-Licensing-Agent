from __future__ import annotations

from openai import OpenAI

from app.config import Settings


def main() -> int:
    settings = Settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not configured.")

    client = OpenAI(api_key=settings.openai_api_key)
    for model_name in (settings.openai_model, settings.openai_transcription_model):
        model = client.models.retrieve(model_name)
        print(f"verified model access: {model.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
