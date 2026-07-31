from time import perf_counter
from typing import Any, BinaryIO

from httpx import AsyncClient, HTTPStatusError, RequestError

from app.config import get_logger

from .schema import (
    AnalyzeAndQuoteResponse,
    FinalQuote,
    QuoteSelectionRequiredResponse,
    TenantAnalysisResponse,
)

logger = get_logger(__name__)


class PricingAgentAPIError(Exception):
    """Raised when the pricing-agent API cannot complete a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
        quote_selection: QuoteSelectionRequiredResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.quote_selection = quote_selection


class PricingAgentClient:
    """Async client for the Skysecure Microsoft Pricing Agent API."""

    def __init__(
        self,
        http_client: AsyncClient,
        base_url: str,
        api_key: str | None = None,
    ) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def health_check(self) -> dict[str, Any]:
        logger.info("Pricing agent health check started")
        return await self._request("GET", "/")

    async def chat(self, message: str) -> dict[str, Any]:
        logger.info("Pricing agent chat started characters=%d", len(message))
        return await self._request("POST", "/chat", json={"message": message})

    async def analyze_tenant_file(
        self,
        file: bytes | BinaryIO,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        logger.info("Pricing agent tenant analysis request filename=%s", filename)
        payload = await self._request(
            "POST",
            "/tenant/analyze",
            files={"file": (filename, file, content_type)},
        )
        return TenantAnalysisResponse.model_validate(payload).model_dump(mode="json")

    async def create_final_quote(
        self,
        product_query: str,
        target_quantity: int,
        existing_quantity: int,
        product_id: str | None = None,
        sku_id: str | int | None = None,
        term_duration: str | None = None,
        billing_plan: str | None = None,
    ) -> dict[str, Any]:
        logger.info(
            "Pricing agent final quote request product=%s target=%d existing=%d sku_id=%s",
            product_query,
            target_quantity,
            existing_quantity,
            sku_id,
        )
        payload = await self._request(
            "POST",
            "/quote/final",
            json={
                "product_query": product_query,
                "target_quantity": target_quantity,
                "existing_quantity": existing_quantity,
                "product_id": product_id,
                "sku_id": str(sku_id) if sku_id is not None else None,
                "term_duration": term_duration,
                "billing_plan": billing_plan,
            },
        )
        return FinalQuote.model_validate(payload).model_dump(mode="json")

    async def analyze_and_quote(
        self,
        file: bytes | BinaryIO,
        filename: str,
        product_query: str,
        target_quantity: int,
        product_id: str | None = None,
        sku_id: str | int | None = None,
        term_duration: str | None = None,
        billing_plan: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> AnalyzeAndQuoteResponse:
        logger.info(
            "Pricing agent analyze-and-quote request product=%s target=%d sku_id=%s",
            product_query,
            target_quantity,
            sku_id,
        )
        data = {
            "product_query": product_query,
            "target_quantity": str(target_quantity),
        }
        optional_fields = {
            "product_id": product_id,
            "sku_id": str(sku_id) if sku_id is not None else None,
            "term_duration": term_duration,
            "billing_plan": billing_plan,
        }
        data.update(
            {name: value for name, value in optional_fields.items() if value is not None}
        )
        payload = await self._request(
            "POST",
            "/analyze-and-quote",
            data=data,
            files={"file": (filename, file, content_type)},
        )
        return AnalyzeAndQuoteResponse.model_validate(payload)

    async def _request(
        self, method: str, path: str, **request_kwargs: Any
    ) -> dict[str, Any]:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        started_at = perf_counter()
        logger.info("Pricing agent HTTP request started method=%s path=%s", method, path)
        try:
            response = await self._http_client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                **request_kwargs,
            )
            response.raise_for_status()
        except HTTPStatusError as error:
            quote_selection = None
            try:
                quote_selection = QuoteSelectionRequiredResponse.model_validate(
                    error.response.json()
                )
            except (ValueError, TypeError):
                pass
            logger.warning(
                "Pricing agent HTTP request rejected method=%s path=%s status=%d duration_ms=%.1f response=%s",
                method,
                path,
                error.response.status_code,
                (perf_counter() - started_at) * 1000,
                error.response.text[:2000],
            )
            raise PricingAgentAPIError(
                "Pricing Agent API rejected the request.",
                status_code=error.response.status_code,
                response_body=error.response.text,
                quote_selection=quote_selection,
            ) from error
        except RequestError as error:
            logger.error(
                "Pricing agent HTTP request failed method=%s path=%s duration_ms=%.1f error=%s",
                method,
                path,
                (perf_counter() - started_at) * 1000,
                error,
            )
            raise PricingAgentAPIError("Unable to reach the Pricing Agent API.") from error

        logger.info(
            "Pricing agent HTTP request completed method=%s path=%s status=%d duration_ms=%.1f",
            method,
            path,
            response.status_code,
            (perf_counter() - started_at) * 1000,
        )

        try:
            payload = response.json()
        except ValueError as error:
            logger.error(
                "Pricing agent returned invalid JSON path=%s status=%d response=%s",
                path,
                response.status_code,
                response.text[:2000],
            )
            raise PricingAgentAPIError(
                "Pricing Agent API returned an invalid JSON response.",
                status_code=response.status_code,
                response_body=response.text,
            ) from error

        if not isinstance(payload, dict):
            logger.error(
                "Pricing agent returned unexpected payload path=%s status=%d",
                path,
                response.status_code,
            )
            raise PricingAgentAPIError(
                "Pricing Agent API returned an unexpected response format.",
                status_code=response.status_code,
                response_body=response.text,
            )

        return payload
