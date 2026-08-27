from dataclasses import dataclass
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from httpx import AsyncClient, HTTPStatusError, RequestError

from app.config import get_logger, opaque_identifier
from app.schema.whatsapp import (
    DocumentContent,
    ImageContent,
    WhatsAppDocumentMessage,
    WhatsAppImageMessage,
    WhatsAppInteractiveMessage,
    WhatsAppTextMessage,
)

logger = get_logger(__name__)
_META_MEDIA_HOST_SUFFIXES = ("facebook.com", "fbcdn.net", "fbsbx.com")


class WhatsAppAPIError(Exception):
    """Raised when the WhatsApp Cloud API cannot send a message."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
        network_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.network_error = network_error


class WhatsAppMediaTooLargeError(WhatsAppAPIError):
    """Raised before an inbound media object can exceed the configured memory limit."""


@dataclass(frozen=True)
class WhatsAppMedia:
    content: bytes
    filename: str
    content_type: str


class WhatsAppClient:
    """Client for sending messages through the WhatsApp Cloud API."""

    def __init__(
        self,
        http_client: AsyncClient,
        access_token: str,
        phone_number_id: str,
        base_url: str = "https://graph.facebook.com",
        api_version: str = "v25.0",
    ) -> None:
        self._http_client = http_client
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._credentials_valid = True
        self._api_url = (
            f"{base_url.rstrip('/')}/{api_version.strip('/')}/"
        )
        self._messages_url = f"{self._api_url}{phone_number_id}/messages"
        self._media_upload_url = f"{self._api_url}{phone_number_id}/media"

    @property
    def credentials_valid(self) -> bool:
        return self._credentials_valid

    async def validate_credentials(self) -> None:
        """Validate that the access token can read the configured phone asset."""
        logger.info("WhatsApp credential validation started")
        try:
            response = await self._http_client.get(
                f"{self._api_url}{self._phone_number_id}",
                params={"fields": "id"},
                headers=self._headers,
            )
            response.raise_for_status()
        except HTTPStatusError as error:
            self._credentials_valid = False
            raise WhatsAppAPIError(
                "WhatsApp credential validation failed.",
                status_code=error.response.status_code,
                response_body=error.response.text,
            ) from None
        except RequestError:
            raise WhatsAppAPIError(
                "Unable to validate WhatsApp credentials.", network_error=True
            ) from None

        self._credentials_valid = True
        logger.info("WhatsApp credential validation completed")

    async def send_message(
        self,
        message: WhatsAppTextMessage
        | WhatsAppInteractiveMessage
        | WhatsAppDocumentMessage
        | WhatsAppImageMessage,
    ) -> dict[str, Any]:
        started_at = perf_counter()
        logger.info("WhatsApp API send request started type=%s", message.type)
        try:
            response = await self._http_client.post(
                self._messages_url,
                json=message.model_dump(mode="json", exclude_none=True),
                headers=self._headers,
            )
            response.raise_for_status()
        except HTTPStatusError as error:
            if self._is_auth_error(error.response):
                self._credentials_valid = False
            logger.warning(
                "WhatsApp API send request rejected status=%d duration_ms=%.1f",
                error.response.status_code,
                (perf_counter() - started_at) * 1000,
            )
            raise WhatsAppAPIError(
                "WhatsApp Cloud API rejected the message request.",
                status_code=error.response.status_code,
                response_body=error.response.text,
            ) from None
        except RequestError as error:
            logger.error(
                "WhatsApp API send request failed duration_ms=%.1f error_type=%s",
                (perf_counter() - started_at) * 1000,
                type(error).__name__,
            )
            raise WhatsAppAPIError(
                "Unable to reach the WhatsApp Cloud API.", network_error=True
            ) from None

        logger.info(
            "WhatsApp API send request completed status=%d duration_ms=%.1f",
            response.status_code,
            (perf_counter() - started_at) * 1000,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise WhatsAppAPIError(
                "WhatsApp Cloud API returned an invalid JSON response.",
                status_code=response.status_code,
                response_body=response.text,
            ) from None

        if not isinstance(payload, dict):
            raise WhatsAppAPIError(
                "WhatsApp Cloud API returned an unexpected response format.",
                status_code=response.status_code,
                response_body=response.text,
            )

        return payload

    async def send_document(
        self,
        *,
        to: str,
        content: bytes,
        filename: str,
        content_type: str = "application/pdf",
        caption: str | None = None,
    ) -> dict[str, Any]:
        media_id = await self._upload_media(
            content=content,
            filename=filename,
            content_type=content_type,
        )
        return await self.send_message(
            WhatsAppDocumentMessage(
                to=to,
                document=DocumentContent(
                    id=media_id,
                    filename=filename,
                    caption=caption,
                ),
            )
        )

    async def send_image(
        self,
        *,
        to: str,
        content: bytes,
        filename: str,
        content_type: str = "image/png",
        caption: str | None = None,
    ) -> dict[str, Any]:
        media_id = await self._upload_media(
            content=content,
            filename=filename,
            content_type=content_type,
        )
        return await self.send_message(
            WhatsAppImageMessage(
                to=to,
                image=ImageContent(id=media_id, caption=caption),
            )
        )

    async def _upload_media(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        try:
            response = await self._http_client.post(
                self._media_upload_url,
                data={"messaging_product": "whatsapp", "type": content_type},
                files={"file": (filename, content, content_type)},
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
            media_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(media_id, str):
                raise WhatsAppAPIError(
                    "WhatsApp Cloud API did not return an uploaded media ID.",
                    status_code=response.status_code,
                    response_body=response.text,
                )
            return media_id
        except HTTPStatusError as error:
            if self._is_auth_error(error.response):
                self._credentials_valid = False
            raise WhatsAppAPIError(
                "WhatsApp Cloud API rejected the media upload.",
                status_code=error.response.status_code,
                response_body=error.response.text,
            ) from None
        except RequestError:
            raise WhatsAppAPIError(
                "Unable to upload media to the WhatsApp Cloud API.",
                network_error=True,
            ) from None
        except ValueError:
            raise WhatsAppAPIError(
                "WhatsApp Cloud API returned invalid media metadata.",
                status_code=response.status_code,
                response_body=response.text,
            ) from None

    async def download_media(
        self,
        media_id: str,
        filename: str,
        content_type: str = "application/octet-stream",
        max_bytes: int | None = None,
    ) -> WhatsAppMedia:
        started_at = perf_counter()
        media_ref = opaque_identifier(media_id)
        logger.info("WhatsApp media metadata request started media_ref=%s", media_ref)
        try:
            metadata_response = await self._http_client.get(
                f"{self._api_url}{media_id}",
                headers=self._headers,
            )
            metadata_response.raise_for_status()
            logger.info(
                "WhatsApp media metadata request completed status=%d",
                metadata_response.status_code,
            )
            metadata = metadata_response.json()
            media_url = metadata.get("url") if isinstance(metadata, dict) else None
            if not isinstance(media_url, str):
                raise WhatsAppAPIError("WhatsApp Cloud API did not return a media URL.")
            parsed_media_url = urlsplit(media_url)
            media_host = (parsed_media_url.hostname or "").casefold()
            trusted_media_host = any(
                media_host == suffix or media_host.endswith(f".{suffix}")
                for suffix in _META_MEDIA_HOST_SUFFIXES
            )
            if parsed_media_url.scheme.casefold() != "https" or not trusted_media_host:
                raise WhatsAppAPIError(
                    "WhatsApp Cloud API returned an unsafe media URL."
                )
        except HTTPStatusError as error:
            logger.warning(
                "WhatsApp media request rejected media_ref=%s status=%d duration_ms=%.1f",
                media_ref,
                error.response.status_code,
                (perf_counter() - started_at) * 1000,
            )
            raise WhatsAppAPIError(
                "WhatsApp Cloud API rejected the media download request.",
                status_code=error.response.status_code,
                response_body=error.response.text,
            ) from None
        except RequestError as error:
            logger.error(
                "WhatsApp media request failed media_ref=%s duration_ms=%.1f "
                "error_type=%s",
                media_ref,
                (perf_counter() - started_at) * 1000,
                type(error).__name__,
            )
            raise WhatsAppAPIError(
                "Unable to download media from the WhatsApp Cloud API.",
                network_error=True,
            ) from None
        except ValueError:
            logger.error("WhatsApp media metadata was invalid media_ref=%s", media_ref)
            raise WhatsAppAPIError(
                "WhatsApp Cloud API returned invalid media metadata.",
                status_code=metadata_response.status_code,
                response_body=metadata_response.text,
            ) from None

        logger.info("WhatsApp media content download started media_ref=%s", media_ref)
        try:
            media_content = bytearray()
            async with self._http_client.stream(
                "GET",
                media_url,
                headers=self._headers,
            ) as media_response:
                media_response.raise_for_status()
                advertised_length = media_response.headers.get("content-length")
                if (
                    max_bytes is not None
                    and advertised_length is not None
                    and int(advertised_length) > max_bytes
                ):
                    raise WhatsAppMediaTooLargeError(
                        f"The uploaded media exceeds the {max_bytes // 1048576} MB limit."
                    )
                async for chunk in media_response.aiter_bytes():
                    if max_bytes is not None and len(media_content) + len(chunk) > max_bytes:
                        raise WhatsAppMediaTooLargeError(
                            f"The uploaded media exceeds the {max_bytes // 1048576} MB limit."
                        )
                    media_content.extend(chunk)
                media_status = media_response.status_code
        except WhatsAppMediaTooLargeError:
            logger.info("WhatsApp media rejected by size limit media_ref=%s", media_ref)
            raise
        except HTTPStatusError as error:
            logger.warning(
                "WhatsApp media content rejected media_ref=%s status=%d duration_ms=%.1f",
                media_ref,
                error.response.status_code,
                (perf_counter() - started_at) * 1000,
            )
            raise WhatsAppAPIError(
                "WhatsApp Cloud API rejected the media download request.",
                status_code=error.response.status_code,
            ) from None
        except RequestError as error:
            logger.error(
                "WhatsApp media content request failed media_ref=%s duration_ms=%.1f "
                "error_type=%s",
                media_ref,
                (perf_counter() - started_at) * 1000,
                type(error).__name__,
            )
            raise WhatsAppAPIError(
                "Unable to download media from the WhatsApp Cloud API.",
                network_error=True,
            ) from None
        except ValueError:
            raise WhatsAppAPIError(
                "WhatsApp Cloud API returned invalid media headers."
            ) from None

        logger.info(
            "WhatsApp media content download completed media_ref=%s status=%d bytes=%d duration_ms=%.1f",
            media_ref,
            media_status,
            len(media_content),
            (perf_counter() - started_at) * 1000,
        )
        metadata_content_type = metadata.get("mime_type")
        return WhatsAppMedia(
            content=bytes(media_content),
            filename=filename,
            content_type=(
                metadata_content_type
                if isinstance(metadata_content_type, str)
                else content_type
            ),
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    @staticmethod
    def _is_auth_error(response: Any) -> bool:
        if response.status_code == 401:
            return True
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        return isinstance(error, dict) and error.get("code") == 190
