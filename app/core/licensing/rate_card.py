from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Protocol

from rapidfuzz import fuzz

from .models import RateCardItem, SkuMatchCandidate


class RateCardError(ValueError):
    pass


@dataclass(frozen=True)
class RateCardPayload:
    content: bytes
    filename: str
    version: str


class RateCardSource(Protocol):
    async def fetch(self) -> RateCardPayload: ...

    async def close(self) -> None: ...


class LocalRateCardSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def fetch(self) -> RateCardPayload:
        return await asyncio.to_thread(self._read)

    def _read(self) -> RateCardPayload:
        try:
            content = self._path.read_bytes()
        except PermissionError as error:
            raise RateCardError(
                f"The local rate-card workbook {self._path} is locked. Close it in "
                "Excel/OneDrive or use the Azure Blob backend."
            ) from error
        stat = self._path.stat()
        version = f"local:{stat.st_mtime_ns}:{stat.st_size}"
        return RateCardPayload(content, self._path.name, version)

    async def close(self) -> None:
        return None


class AzureBlobRateCardSource:
    """Reads the maintained workbook directly from Blob using MI or a dev connection string."""

    def __init__(
        self,
        *,
        container_name: str,
        blob_name: str,
        account_url: str | None = None,
        connection_string: str | None = None,
    ) -> None:
        if not (connection_string or account_url):
            raise ValueError("An account URL or connection string is required.")

        from azure.storage.blob.aio import BlobServiceClient

        self._credential = None
        if connection_string:
            self._service = BlobServiceClient.from_connection_string(connection_string)
        else:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            self._service = BlobServiceClient(
                account_url=account_url,
                credential=self._credential,
            )
        self._blob = self._service.get_blob_client(container_name, blob_name)
        self._blob_name = blob_name

    async def fetch(self) -> RateCardPayload:
        properties = await self._blob.get_blob_properties()
        stream = await self._blob.download_blob()
        content = await stream.readall()
        version = str(properties.etag or properties.last_modified or len(content))
        return RateCardPayload(content, self._blob_name, version)

    async def close(self) -> None:
        await self._service.close()
        if self._credential is not None:
            await self._credential.close()


def normalize_product_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    aliases = (
        (r"\bm365\b", "microsoft 365"),
        (r"\bo365\b", "office 365"),
        (r"\bems\b", "enterprise mobility security"),
        (r"\bplan one\b", "plan 1"),
        (r"\bplan two\b", "plan 2"),
    )
    for pattern, replacement in aliases:
        normalized = re.sub(pattern, replacement, normalized)
    return normalized


_GENERIC_SKU_TOKENS = {
    "license",
    "licenses",
    "licence",
    "licences",
    "plan",
    "plans",
    "product",
    "sku",
    "subscription",
    "subscriptions",
}
_VARIANT_MARKERS = (
    "education",
    "student",
    "faculty",
    "non profit",
    "nonprofit",
    "government",
    "charity",
    "three year",
    "3 year",
    "36 month",
    "unattended",
    "student use benefit",
    "no teams",
)
_FAMILY_MARKERS = (
    # Specific product families must precede the suite names embedded in their
    # titles (for example, Defender for Office 365 and Microsoft 365 Copilot).
    ("microsoft defender", "defender"),
    ("defender", "defender"),
    ("copilot", "copilot"),
    ("dynamics 365", "dynamics-365"),
    ("enterprise mobility security", "enterprise-mobility-security"),
    ("power bi", "power-bi"),
    ("power apps", "power-platform"),
    ("power automate", "power-platform"),
    ("microsoft 365", "microsoft-365"),
    ("office 365", "office-365"),
    ("teams", "teams"),
    ("visio", "visio"),
    ("project", "project"),
    ("intune", "intune"),
    ("entra", "entra"),
    ("windows", "windows"),
)
_AMBIGUOUS_TIER_FAMILY_PRIORITY = {
    "microsoft-365": 0,
    "office-365": 1,
    "enterprise-mobility-security": 2,
    "defender": 3,
    "dynamics-365": 4,
}


def _tier_tokens(normalized: str) -> set[str]:
    return set(re.findall(r"\b(?:a|e|f)\d+\b", normalized))


def _product_family_key(normalized: str) -> str | None:
    for marker, family in _FAMILY_MARKERS:
        if marker in normalized:
            return family
    return None


def _unrequested_variant_count(query: str, title: str) -> int:
    return sum(marker in title and marker not in query for marker in _VARIANT_MARKERS)


def _base_suite_penalty(title: str) -> int:
    """Prefer a suite itself over similarly named add-ons for a tier-only query."""

    tier = next(iter(_tier_tokens(title)), "")
    family = _product_family_key(title)
    if not tier or family not in {
        "microsoft-365",
        "office-365",
        "enterprise-mobility-security",
    }:
        return 1
    permitted = {
        "microsoft",
        "office",
        "365",
        "enterprise",
        "mobility",
        "security",
        tier,
        "without",
        "audio",
        "conferencing",
        "no",
        "teams",
    }
    return int(bool(set(title.split()) - permitted))


@dataclass(frozen=True)
class SkuIdentity:
    product_id: str
    sku_id: str
    sku_title: str


class RateCardCatalog:
    def __init__(self, items: Sequence[RateCardItem], version: str) -> None:
        if not items:
            raise RateCardError("The Outcome Sheet contains no priceable rows.")
        self.items = tuple(items)
        self.version = version
        identities: dict[tuple[str, str, str], SkuIdentity] = {}
        for item in items:
            key = (item.product_id, item.sku_id, item.sku_title)
            identities.setdefault(key, SkuIdentity(*key))
        self.identities = tuple(identities.values())
        self._normalized_titles = {
            index: normalize_product_title(identity.sku_title)
            for index, identity in enumerate(self.identities)
        }
        self._items_by_identity: dict[tuple[str, str, str], list[RateCardItem]] = {}
        for item in items:
            key = (item.product_id, item.sku_id, item.sku_title)
            self._items_by_identity.setdefault(key, []).append(item)

    def candidates(
        self,
        query: str,
        *,
        product_id: str | None = None,
        sku_id: str | None = None,
        limit: int = 3,
    ) -> list[SkuMatchCandidate]:
        if (
            product_id
            and sku_id
            and not _is_placeholder_identifier(product_id)
            and not _is_placeholder_identifier(sku_id)
        ):
            exact_ids = [
                identity
                for identity in self.identities
                if identity.product_id.casefold() == product_id.casefold()
                and identity.sku_id.casefold() == sku_id.casefold()
            ]
            if exact_ids:
                normalized_query = normalize_product_title(query)
                exact_title = [
                    identity
                    for identity in exact_ids
                    if normalize_product_title(identity.sku_title) == normalized_query
                ]
                if exact_title:
                    return [
                        self._candidate(identity, 100.0)
                        for identity in exact_title[:limit]
                    ]
                if len(exact_ids) == 1:
                    return [self._candidate(exact_ids[0], 100.0)]
                best = max(
                    exact_ids,
                    key=lambda identity: fuzz.WRatio(
                        normalized_query,
                        normalize_product_title(identity.sku_title),
                    ),
                )
                return [self._candidate(best, 100.0)]

        normalized = normalize_product_title(query)
        exact = [
            self.identities[index]
            for index, title in self._normalized_titles.items()
            if title == normalized
        ]
        if exact:
            # A workbook can expose the same display title under monthly and annual
            # SKU identities. The seller cannot distinguish identical labels, so use
            # the maintained annual Commercial identity when one exists.
            return [self._candidate(self._preferred_identity(exact), 100.0)]

        query_tokens = set(normalized.split())
        tiers = _tier_tokens(normalized)
        query_family = _product_family_key(normalized)
        pool = list(self.identities)
        if tiers:
            tier_pool = [
                identity
                for identity in pool
                if tiers.issubset(_tier_tokens(normalize_product_title(identity.sku_title)))
            ]
            if not tier_pool:
                return []
            pool = tier_pool
        if query_family:
            family_pool = [
                identity
                for identity in pool
                if _product_family_key(normalize_product_title(identity.sku_title))
                == query_family
            ]
            if not family_pool:
                return []
            pool = family_pool

        ranked: list[tuple[tuple[object, ...], SkuIdentity, float]] = []
        for identity in pool:
            title = normalize_product_title(identity.sku_title)
            title_tokens = set(title.split())
            informative_query = query_tokens - _GENERIC_SKU_TOKENS
            overlap = len(informative_query & title_tokens)
            if informative_query and overlap == 0:
                continue
            score = float(fuzz.WRatio(normalized, title))
            if informative_query:
                score += 8.0 * overlap / len(informative_query)
            score -= 6.0 * _unrequested_variant_count(normalized, title)
            score = max(0.0, min(99.0, score))
            if score < 55.0:
                continue
            ranked.append(
                (
                    self._candidate_rank(
                        identity,
                        normalized_query=normalized,
                        query_family=query_family,
                        has_tier=bool(tiers),
                        score=score,
                    ),
                    identity,
                    score,
                )
            )
        ranked.sort(key=lambda item: item[0])

        result: list[SkuMatchCandidate] = []
        seen_titles: set[str] = set()
        seen_families: set[str] = set()
        family_choice = bool(tiers) and not query_family
        for _rank, identity, score in ranked:
            title_key = normalize_product_title(identity.sku_title)
            family_key = _product_family_key(title_key) or title_key
            if title_key in seen_titles:
                continue
            if family_choice and family_key in seen_families:
                continue
            seen_titles.add(title_key)
            seen_families.add(family_key)
            result.append(self._candidate(identity, score))
            if len(result) >= limit:
                break
        return result

    def _preferred_identity(self, identities: Sequence[SkuIdentity]) -> SkuIdentity:
        return min(identities, key=self._identity_preference)

    def _identity_preference(self, identity: SkuIdentity) -> tuple[object, ...]:
        rows = self._items_by_identity.get(
            (identity.product_id, identity.sku_id, identity.sku_title),
            [],
        )
        annual = any(
            row.term_duration.casefold() == "p1y"
            and row.billing_plan.casefold() == "annual"
            for row in rows
        )
        commercial = any(
            (row.segment or "").casefold() == "commercial"
            and row.term_duration.casefold() == "p1y"
            and row.billing_plan.casefold() == "annual"
            for row in rows
        )
        priceable = any(
            row.marketplace_price > 0
            and row.term_duration.casefold() == "p1y"
            and row.billing_plan.casefold() == "annual"
            for row in rows
        )
        return (
            not commercial,
            not annual,
            not priceable,
            _unrequested_variant_count("", normalize_product_title(identity.sku_title)),
            identity.sku_id.casefold(),
        )

    def _candidate_rank(
        self,
        identity: SkuIdentity,
        *,
        normalized_query: str,
        query_family: str | None,
        has_tier: bool,
        score: float,
    ) -> tuple[object, ...]:
        title = normalize_product_title(identity.sku_title)
        family = _product_family_key(title)
        family_priority = (
            _AMBIGUOUS_TIER_FAMILY_PRIORITY.get(family or "", 99)
            if has_tier and query_family is None
            else 0
        )
        return (
            family_priority,
            _unrequested_variant_count(normalized_query, title),
            _base_suite_penalty(title),
            *self._identity_preference(identity),
            -score,
            len(title),
            title,
        )

    def price_rows(
        self,
        *,
        product_id: str,
        sku_id: str,
        sku_title: str | None = None,
        term_duration: str | None = None,
        billing_plan: str | None = None,
        segment: str | None = None,
    ) -> list[RateCardItem]:
        rows = [
            item
            for item in self.items
            if item.product_id.casefold() == product_id.casefold()
            and item.sku_id.casefold() == sku_id.casefold()
        ]
        if sku_title:
            normalized = normalize_product_title(sku_title)
            title_rows = [
                item
                for item in rows
                if normalize_product_title(item.sku_title) == normalized
            ]
            if title_rows:
                rows = title_rows
        if term_duration:
            rows = [
                item
                for item in rows
                if item.term_duration.casefold() == term_duration.casefold()
            ]
        if billing_plan:
            rows = [
                item
                for item in rows
                if item.billing_plan.casefold() == billing_plan.casefold()
            ]
        if segment:
            segment_rows = [
                item
                for item in rows
                if item.segment and item.segment.casefold() == segment.casefold()
            ]
            if segment_rows:
                rows = segment_rows
        return rows

    def higher_tier_candidates(
        self,
        current_title: str,
        *,
        limit: int = 3,
    ) -> list[SkuMatchCandidate]:
        """Return same-family higher-tier catalogue options for seller review only."""

        normalized = normalize_product_title(current_title)
        family = _product_family_key(normalized)
        current_tiers = _tier_tokens(normalized)
        if family is None or len(current_tiers) != 1:
            return []
        current_tier = next(iter(current_tiers))
        tier_match = re.fullmatch(r"([aef])(\d+)", current_tier)
        if tier_match is None:
            return []
        tier_prefix, current_level_text = tier_match.groups()
        current_level = int(current_level_text)
        by_tier: dict[int, list[SkuIdentity]] = {}
        for identity in self.identities:
            title = normalize_product_title(identity.sku_title)
            if _product_family_key(title) != family:
                continue
            tiers = _tier_tokens(title)
            if len(tiers) != 1:
                continue
            match = re.fullmatch(r"([aef])(\d+)", next(iter(tiers)))
            if match is None or match.group(1) != tier_prefix:
                continue
            level = int(match.group(2))
            if level <= current_level:
                continue
            by_tier.setdefault(level, []).append(identity)

        result: list[SkuMatchCandidate] = []
        for level in sorted(by_tier):
            identity = min(
                by_tier[level],
                key=lambda item: (
                    _unrequested_variant_count(
                        normalized,
                        normalize_product_title(item.sku_title),
                    ),
                    _base_suite_penalty(normalize_product_title(item.sku_title)),
                    *self._identity_preference(item),
                    len(item.sku_title),
                ),
            )
            result.append(self._candidate(identity, 99.0))
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _candidate(identity: SkuIdentity, confidence: float) -> SkuMatchCandidate:
        return SkuMatchCandidate(
            product_id=identity.product_id,
            sku_id=identity.sku_id,
            sku_title=identity.sku_title,
            confidence=confidence,
        )


class RateCardProvider:
    def __init__(
        self,
        source: RateCardSource,
        *,
        sheet_name: str = "Outcome Sheet",
        refresh_seconds: float = 300,
    ) -> None:
        self._source = source
        self._sheet_name = sheet_name
        self._refresh_seconds = refresh_seconds
        self._catalog: RateCardCatalog | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, force: bool = False) -> RateCardCatalog:
        if (
            not force
            and self._catalog is not None
            and monotonic() - self._loaded_at < self._refresh_seconds
        ):
            return self._catalog
        async with self._lock:
            if (
                not force
                and self._catalog is not None
                and monotonic() - self._loaded_at < self._refresh_seconds
            ):
                return self._catalog
            payload = await self._source.fetch()
            items = await asyncio.to_thread(
                parse_rate_card,
                payload.content,
                payload.filename,
                self._sheet_name,
            )
            digest = hashlib.sha256(payload.content).hexdigest()[:16]
            self._catalog = RateCardCatalog(
                items,
                version=f"{payload.version}:{digest}",
            )
            self._loaded_at = monotonic()
            return self._catalog

    async def close(self) -> None:
        await self._source.close()


_HEADERS = {
    "productid": "product_id",
    "skuid": "sku_id",
    "skutitle": "sku_title",
    "contracttype": "contract_type",
    "termduration": "term_duration",
    "billingplan": "billing_plan",
    "segment": "segment",
    "erp": "erp_price",
    "erpprice": "erp_price",
    "catalogueprice": "catalogue_price",
    "unitpricecatalogue": "catalogue_price",
    "promoname": "promo_name",
    "promo": "promo_percentage",
    "promoifnewtoms": "promo_percentage",
    "customereligibility": "customer_eligibility",
    "newtomicrosoftrequired": "new_to_microsoft_required",
    "minimumseats": "minimum_seats",
    "maximumseats": "maximum_seats",
    "geography": "geography",
    "nettoms": "net_to_ms",
    "expectedpartnerpricingwithpromo": "partner_price_with_promo",
    "expectedpartnerpricingwithoutpromo": "partner_price_without_promo",
    "distributorlandingprice": "distributor_landing_price",
    "partnerlandingpricefromdistributor": "partner_landing_price",
    "partnerscostofproduct": "partner_cost",
    "partnerbestoffer": "partner_best_offer",
    "priceonmarketplace": "marketplace_price",
    "initialquotewithpromo": "initial_quote_with_promo",
    "initialquotewithoutpromo": "initial_quote_without_promo",
}

_BASE_REQUIRED = {
    "product_id",
    "sku_id",
    "sku_title",
    "term_duration",
    "billing_plan",
}


def parse_rate_card(
    content: bytes,
    filename: str,
    sheet_name: str = "Outcome Sheet",
) -> list[RateCardItem]:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".xlsx":
        rows = _xlsx_rows(content, sheet_name)
    elif suffix == ".csv":
        rows = _csv_rows(content)
    else:
        raise RateCardError("The rate card must be an .xlsx workbook or .csv file.")
    return _parse_rows(rows)


def _xlsx_rows(content: bytes, sheet_name: str) -> Iterable[Sequence[object]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise RateCardError(f"Workbook sheet {sheet_name!r} was not found.")
        yield from workbook[sheet_name].iter_rows(values_only=True)
    finally:
        workbook.close()


def _csv_rows(content: bytes) -> Iterable[Sequence[object]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise RateCardError("The rate-card CSV must be UTF-8 encoded.") from error
    yield from csv.reader(io.StringIO(text))


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_placeholder_identifier(value: str) -> bool:
    return value.strip().casefold() in {"0", "-", "n/a", "na", "none", "unmapped"}


def _parse_rows(rows: Iterable[Sequence[object]]) -> list[RateCardItem]:
    iterator = iter(rows)
    try:
        raw_headers = next(iterator)
    except StopIteration as error:
        raise RateCardError("The rate card is empty.") from error

    columns: dict[int, str] = {}
    for index, header in enumerate(raw_headers):
        field = _HEADERS.get(_header_key(header))
        if field:
            columns[index] = field
    available_fields = set(columns.values())
    missing = sorted(_BASE_REQUIRED - available_fields)
    if missing:
        raise RateCardError("Missing rate-card columns: " + ", ".join(missing))
    has_legacy_quotes = {
        "initial_quote_with_promo",
        "initial_quote_without_promo",
    }.issubset(available_fields)
    has_final_offer = "partner_best_offer" in available_fields
    if not has_legacy_quotes and not has_final_offer:
        raise RateCardError(
            "Missing rate-card price columns: provide a supported commercial-price field."
        )

    items: list[RateCardItem] = []
    for row_number, values in enumerate(iterator, start=2):
        record = {
            field: values[index] if index < len(values) else None
            for index, field in columns.items()
        }
        if not any(value not in (None, "") for value in record.values()):
            continue
        for field in ("product_id", "sku_id", "sku_title", "term_duration", "billing_plan"):
            raw_value = record.get(field)
            record[field] = (
                "" if raw_value in (None, "") else str(raw_value).strip()
            )
            if not record[field]:
                raise RateCardError(f"Rate-card row {row_number} has an empty {field}.")

        # Microsoft SKU V5.0 makes the maintained Final Output Sheet authoritative.
        # Its direct seller offer is the quoted commercial value. Promotional rows are
        # deliberately not exposed as a non-promotional price: the seller must first
        # confirm eligibility, while standard rows remain usable in either mode.
        if has_final_offer and not has_legacy_quotes:
            offer = record.get("partner_best_offer")
            normalized_offer = _decimal(offer, "partner_best_offer", row_number)
            promo_percentage = _decimal(
                record.get("promo_percentage"), "promo_percentage", row_number
            )
            promo_name = str(record.get("promo_name") or "").strip()
            is_promotional = promo_percentage > 0 or promo_name not in {"", "0"}
            offer_is_available = offer not in (None, "") and normalized_offer > 0
            record["initial_quote_with_promo"] = (
                offer if is_promotional and offer_is_available else None
            )
            record["initial_quote_without_promo"] = (
                offer if not is_promotional and offer_is_available else None
            )

        record["initial_quote_with_promo_available"] = record.get(
            "initial_quote_with_promo"
        ) not in (None, "")
        record["initial_quote_without_promo_available"] = record.get(
            "initial_quote_without_promo"
        ) not in (None, "")
        for field in (
            "erp_price",
            "catalogue_price",
            "promo_percentage",
            "net_to_ms",
            "partner_price_with_promo",
            "partner_price_without_promo",
            "distributor_landing_price",
            "partner_landing_price",
            "partner_cost",
            "partner_best_offer",
            "marketplace_price",
            "initial_quote_with_promo",
            "initial_quote_without_promo",
        ):
            record[field] = _decimal(record.get(field), field, row_number)
        for field in (
            "contract_type",
            "segment",
            "promo_name",
            "customer_eligibility",
            "new_to_microsoft_required",
            "geography",
        ):
            value = record.get(field)
            normalized = str(value).strip() if value not in (None, "", 0, "0") else None
            record[field] = normalized
        for field in ("minimum_seats", "maximum_seats"):
            record[field] = _optional_int(record.get(field), field, row_number)
        items.append(RateCardItem(**record, source_row_number=row_number))
    return items


def _decimal(value: object, field: str, row_number: int) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise RateCardError(
            f"Rate-card row {row_number} has an invalid {field}."
        ) from error
    if result < 0:
        raise RateCardError(f"Rate-card row {row_number} has a negative {field}.")
    # Excel formulas commonly surface harmless binary-float tails such as
    # 1746.3600000000001. Normalize at the ingestion boundary while retaining
    # more precision than the two-decimal commercial output.
    return result.quantize(Decimal("0.000001"))


def _optional_int(value: object, field: str, row_number: int) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise RateCardError(
            f"Rate-card row {row_number} has an invalid {field}."
        ) from error
    if result < 0 or result != result.to_integral_value():
        raise RateCardError(
            f"Rate-card row {row_number} has an invalid {field}."
        )
    return int(result)
