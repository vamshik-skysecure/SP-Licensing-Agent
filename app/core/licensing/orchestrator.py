from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable, Literal
from uuid import uuid4

from .analysis import LicenseAnalyzer, SkuMatchSelection
from .candidate_policy import requires_candidate_narrowing
from .models import (
    CommercialComparison,
    CommercialScenario,
    EstateStatus,
    LicenseEstate,
    MigrationDisposition,
    NormalizedLicenseLine,
    PendingDialogue,
    PendingSkuChange,
    ParsedLicenseRow,
    ScenarioType,
    SellerProvidedDetail,
    SkuChangeResult,
    WorkflowSession,
    WorkflowStage,
)
from .rate_card import RateCardCatalog, RateCardProvider, normalize_product_title
from .scenarios import ScenarioEngine, ScenarioError, SkuSelector
from .store import WorkflowConflictError, WorkflowStore


# A claimed webhook turn may legitimately spend longer than the conversational TTL in
# media parsing, model inference, rendering, or outbound delivery.  Keep that ownership
# local to the current async context: unrelated turns must still observe the normal TTL.
# The tuple also includes the opaque message digest so nested claims cannot deactivate one
# another accidentally.
_ACTIVE_TURN_CLAIMS: ContextVar[
    frozenset[tuple[str, str, str]]
] = ContextVar("licensing_active_turn_claims", default=frozenset())


@dataclass(frozen=True)
class CatalogOffer:
    product_id: str
    sku_id: str
    sku_title: str
    unit_price: Decimal
    term_duration: str
    billing_plan: str


class LicensingOrchestrator:
    def __init__(
        self,
        *,
        analyzer: LicenseAnalyzer,
        rate_cards: RateCardProvider,
        scenarios: ScenarioEngine,
        store: WorkflowStore,
        default_term_duration: str,
        default_billing_plan: str,
        default_segment: str,
    ) -> None:
        self._analyzer = analyzer
        self._rate_cards = rate_cards
        self._scenarios = scenarios
        self._store = store
        self._default_term_duration = default_term_duration
        self._default_billing_plan = default_billing_plan
        self._default_segment = default_segment
        self._turn_context_owner = uuid4().hex

    @staticmethod
    def thread_id(sender: str) -> str:
        digest = hashlib.sha256(sender.lstrip("+").encode("utf-8")).hexdigest()[:24]
        return f"wa-{digest}"

    @staticmethod
    def processed_message_id(message_id: str) -> str:
        return hashlib.sha256(message_id.encode("utf-8")).hexdigest()

    def _activate_turn_claim(self, thread_id: str, message_digest: str) -> None:
        claims = _ACTIVE_TURN_CLAIMS.get()
        _ACTIVE_TURN_CLAIMS.set(
            claims | {(self._turn_context_owner, thread_id, message_digest)}
        )

    def _deactivate_turn_claim(self, thread_id: str, message_digest: str) -> None:
        claim = (self._turn_context_owner, thread_id, message_digest)
        claims = _ACTIVE_TURN_CLAIMS.get()
        if claim in claims:
            _ACTIVE_TURN_CLAIMS.set(claims - {claim})

    def _has_active_turn_claim(self, thread_id: str) -> bool:
        orchestrator_id = self._turn_context_owner
        return any(
            owner == orchestrator_id and claimed_thread == thread_id
            for owner, claimed_thread, _message_digest in _ACTIVE_TURN_CLAIMS.get()
        )

    def end_message_processing_context(self, sender: str, message_id: str) -> None:
        """Drop task-local turn ownership without changing persisted replay state.

        The webhook service calls this in ``finally`` so cancellation, outbound delivery
        failures, and receipt-write failures cannot leak an active-turn read into the next
        message handled by the same worker task.
        """

        self._deactivate_turn_claim(
            self.thread_id(sender),
            self.processed_message_id(message_id),
        )

    @staticmethod
    def _same_catalogue_line_identity(
        first: NormalizedLicenseLine,
        second: NormalizedLicenseLine,
    ) -> bool:
        first_product_id = (first.product_id or "").casefold()
        first_sku_id = (first.sku_id or "").casefold()
        second_product_id = (second.product_id or "").casefold()
        second_sku_id = (second.sku_id or "").casefold()
        if (first_product_id, first_sku_id) != (
            second_product_id,
            second_sku_id,
        ):
            return False
        if first_product_id and first_sku_id:
            return True
        return normalize_product_title(first.display_title) == normalize_product_title(
            second.display_title
        )

    @staticmethod
    def _ensure_proposal_mutation_allowed(session: WorkflowSession) -> None:
        """Protect validation gates and finalized proposals at the domain boundary.

        Service-layer checks improve the seller experience, but every state-changing
        orchestration path must enforce the same invariant inside its optimistic-
        concurrency mutation. Otherwise a direct caller, stale interactive reply, or
        retry can reopen a proposal after validation has started or completed.
        """

        if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
            raise ScenarioError(
                "Final seller validation is pending. Cancel finalization before changing "
                "the proposal."
            )
        if session.stage == WorkflowStage.FINALIZED:
            raise ScenarioError(
                "This proposal is finalized. Start a fresh requirement before changing it."
            )

    @staticmethod
    def _requirement_fingerprint(estate: LicenseEstate) -> str:
        """Return a stable commercial snapshot for a pending requirement choice."""

        payload = {
            "rate_card_version": estate.rate_card_version,
            "lines": [line.model_dump(mode="json") for line in estate.lines],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _ensure_no_unfinished_work(
        session: WorkflowSession,
        *,
        action: str,
        allow_pending_matches: bool = False,
        allow_capture_messages: bool = False,
    ) -> None:
        blockers: list[str] = []
        if session.capture_messages and not allow_capture_messages:
            blockers.append("unfinished product or quantity details")
        if session.pending_dialogue is not None:
            blockers.append("an unanswered clarification")
        if session.pending_sku_change is not None:
            blockers.append("an unconfirmed SKU choice")
        if (
            not allow_pending_matches
            and session.estate is not None
            and session.estate.pending_lines
        ):
            blockers.append("an unresolved catalogue match")
        if blockers:
            raise ScenarioError(
                f"Resolve or explicitly cancel {', '.join(blockers)} before {action}."
            )

    @classmethod
    def _ensure_new_analysis_allowed(
        cls,
        session: WorkflowSession,
        *,
        allow_capture_messages: bool = False,
    ) -> None:
        """Require an explicit reset before replacing any active workflow state."""

        if (
            session.stage != WorkflowStage.AWAITING_UPLOAD
            or session.estate is not None
            or session.scenarios
            or session.active_scenario is not None
            or session.confirmed_as_is is not None
            or (session.capture_messages and not allow_capture_messages)
            or session.pending_dialogue is not None
            or session.pending_sku_change is not None
        ):
            raise ScenarioError(
                "A licensing requirement is already active. Resolve it, or explicitly "
                "start fresh before replacing it with another file or requirement."
            )

    async def get_session(self, sender: str) -> WorkflowSession | None:
        thread_id = self.thread_id(sender)
        if self._has_active_turn_claim(thread_id):
            session, _ = await self._get_raw_session(sender)
        else:
            session, _ = await self._store.get(thread_id)
        return session

    async def _get_raw_session(
        self, sender: str
    ) -> tuple[WorkflowSession | None, str | None]:
        """Read message history even after the five-minute conversation TTL."""

        raw_reader = getattr(self._store, "get_raw", None)
        if raw_reader is None:
            # Compatibility for small test doubles and third-party stores. Production
            # stores implement get_raw so inbox de-duplication survives session expiry.
            return await self._store.get(self.thread_id(sender))
        return await raw_reader(self.thread_id(sender))

    async def affordable_catalog_offers(
        self,
        *,
        budget: Decimal,
        price_basis: Literal["marketplace", "distributor_expected"],
        product_query: str = "",
        limit: int = 8,
    ) -> list[CatalogOffer]:
        """Return unambiguous one-year annual offers closest to a stated unit budget."""

        if budget <= 0 or limit <= 0:
            return []
        catalog = await self._rate_cards.get()
        allowed: set[tuple[str, str, str]] | None = None
        if product_query.strip():
            candidates = catalog.candidates(product_query, limit=None)
            allowed = {
                (
                    candidate.product_id.casefold(),
                    candidate.sku_id.casefold(),
                    normalize_product_title(candidate.sku_title),
                )
                for candidate in candidates
            }
            if not allowed:
                return []

        grouped: dict[tuple[str, str, str], list] = {}
        for item in catalog.items:
            if item.term_duration.casefold() != self._default_term_duration.casefold():
                continue
            if item.billing_plan.casefold() != self._default_billing_plan.casefold():
                continue
            identity = (
                item.product_id.casefold(),
                item.sku_id.casefold(),
                normalize_product_title(item.sku_title),
            )
            if allowed is not None and identity not in allowed:
                continue
            grouped.setdefault(identity, []).append(item)

        offers: list[CatalogOffer] = []
        for rows in grouped.values():
            exact_segment = [
                row
                for row in rows
                if row.segment
                and row.segment.casefold() == self._default_segment.casefold()
            ]
            selected_rows = exact_segment or rows
            prices = {
                (
                    row.distributor_price
                    if price_basis == "distributor_expected"
                    else row.marketplace_price
                )
                for row in selected_rows
            }
            prices = {price for price in prices if price > Decimal("0")}
            if len(prices) != 1:
                # A blank or conflicting commercial value is not safe to present.
                continue
            selected_price = next(iter(prices))
            if selected_price > budget:
                continue
            row = selected_rows[0]
            offers.append(
                CatalogOffer(
                    product_id=row.product_id,
                    sku_id=row.sku_id,
                    sku_title=row.sku_title,
                    unit_price=selected_price,
                    term_duration=row.term_duration,
                    billing_plan=row.billing_plan,
                )
            )
        offers.sort(key=lambda item: (-item.unit_price, item.sku_title.casefold()))
        return offers[:limit]

    async def catalog_candidates(self, query: str):
        """Expose read-only matching for service-layer ambiguity guards."""

        catalog = await self._rate_cards.get()
        return catalog.candidates(query, limit=None)

    async def reset_expired_session(self, sender: str) -> bool:
        """Atomically replace expired state and report whether a reset occurred."""

        thread_id = self.thread_id(sender)
        for _ in range(3):
            session, version = await self._store.get(thread_id)
            if session is not None or version is None:
                return False
            expired, raw_version = await self._get_raw_session(sender)
            if raw_version != version:
                continue
            fresh = WorkflowSession(
                id=thread_id,
                thread_id=thread_id,
                # Keep the persisted compatibility field opaque. The raw WhatsApp
                # number is needed only while handling the current request.
                sender=thread_id,
                processed_message_ids=(
                    list(expired.processed_message_ids[-1000:]) if expired else []
                ),
                inflight_message_ids=(
                    list(expired.inflight_message_ids[-1000:]) if expired else []
                ),
                failure_notified_message_ids=(
                    list(expired.failure_notified_message_ids[-1000:]) if expired else []
                ),
            )
            try:
                await self._store.save(fresh, version)
                return True
            except WorkflowConflictError:
                continue
        raise WorkflowConflictError(
            "The expired workflow changed concurrently; retry the last operation."
        )

    async def remember_capture_message(self, sender: str, message: str) -> list[str]:
        """Persist bounded seller context for an incomplete typed requirement."""

        cleaned = " ".join(message.strip().split())[:2000]
        if not cleaned:
            raise ScenarioError("Please send the missing requirement detail.")
        result: list[str] = []

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            self._ensure_proposal_mutation_allowed(session)
            if session.pending_sku_change is not None:
                raise ScenarioError(
                    "Confirm or cancel the pending SKU choice before starting another "
                    "licence entry."
                )
            if session.pending_dialogue is not None:
                raise ScenarioError(
                    "Answer or cancel the pending clarification before starting another "
                    "licence entry."
                )
            result = [*session.capture_messages, cleaned][-8:]
            return session.model_copy(
                update={
                    "capture_messages": result,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return result

    async def reset_session(self, sender: str) -> WorkflowSession:
        """Clear commercial state while retaining message de-duplication history."""

        now = datetime.now(UTC)

        def update(session: WorkflowSession) -> WorkflowSession:
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_UPLOAD,
                    "estate": None,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "requirement_confirmed": False,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
                    "pending_match_prompt_suspended": False,
                    "capture_messages": [],
                    "created_at": now,
                    "updated_at": now,
                }
            )

        return await self._mutate(sender, update)

    async def clear_capture_messages(self, sender: str) -> WorkflowSession:
        """Discard only an incomplete typed fragment, preserving the current draft."""

        def update(session: WorkflowSession) -> WorkflowSession:
            return session.model_copy(
                update={
                    "capture_messages": [],
                    "updated_at": datetime.now(UTC),
                }
            )

        return await self._mutate(sender, update)

    async def set_pending_dialogue(
        self,
        sender: str,
        pending: PendingDialogue,
    ) -> WorkflowSession:
        """Persist a clarification so a short reply is interpreted in context."""

        return await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "pending_dialogue": pending,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    async def clear_pending_dialogue(self, sender: str) -> WorkflowSession:
        return await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "pending_dialogue": None,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    async def record_pending_dialogue_failure(self, sender: str) -> int:
        """Count one unanswered clarification without replaying it indefinitely."""

        attempts = 0

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal attempts
            pending = session.pending_dialogue
            if pending is None:
                return session
            attempts = min(pending.failed_attempts + 1, 2)
            return session.model_copy(
                update={
                    "pending_dialogue": pending.model_copy(
                        update={"failed_attempts": attempts}
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return attempts

    async def record_pending_sku_change_failure(self, sender: str) -> int:
        """Atomically count one unanswered pending SKU-choice attempt."""

        attempts = 0

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal attempts
            pending = session.pending_sku_change
            if pending is None:
                return session
            attempts = min(pending.failed_attempts + 1, 2)
            return session.model_copy(
                update={
                    "pending_sku_change": pending.model_copy(
                        update={"failed_attempts": attempts}
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return attempts

    async def set_pending_match_prompt_suspended(
        self,
        sender: str,
        suspended: bool,
    ) -> WorkflowSession:
        """Pause a repeated catalogue gate while retaining every unresolved line."""

        return await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "pending_match_prompt_suspended": suspended,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    async def has_processed(self, sender: str, message_id: str) -> bool:
        session, _ = await self._get_raw_session(sender)
        if session is None:
            return False
        digest = self.processed_message_id(message_id)
        # Accept legacy raw entries during a rolling deployment; the next mutation
        # replaces the retained history with opaque digests.
        return message_id in session.processed_message_ids or digest in session.processed_message_ids

    async def claim_message_processing(
        self,
        sender: str,
        message_id: str,
    ) -> Literal["claimed", "inflight", "processed"]:
        """Atomically claim an inbound message before any commercial mutation.

        A surviving in-flight digest is intentionally conservative: a restarted worker
        cannot know whether the prior process terminated before or after committing a
        mutation, so it must not execute the same instruction automatically again.
        """

        thread_id = self.thread_id(sender)
        digest = self.processed_message_id(message_id)
        for _ in range(3):
            session, version = await self._get_raw_session(sender)
            if session is None:
                session = WorkflowSession(
                    id=thread_id,
                    thread_id=thread_id,
                    sender=thread_id,
                )
            processed = {
                value
                if re.fullmatch(r"[0-9a-f]{64}", value)
                else self.processed_message_id(value)
                for value in session.processed_message_ids
            }
            if digest in processed:
                return "processed"
            if digest in session.inflight_message_ids:
                return "inflight"
            updated = session.model_copy(
                update={
                    "sender": thread_id,
                    "inflight_message_ids": [
                        *session.inflight_message_ids[-999:],
                        digest,
                    ],
                    "updated_at": datetime.now(UTC),
                }
            )
            try:
                await self._store.save(updated, version)
                self._activate_turn_claim(thread_id, digest)
                return "claimed"
            except WorkflowConflictError:
                continue
        raise WorkflowConflictError(
            "The inbound message claim changed concurrently; retry delivery."
        )

    async def has_inflight_message(self, sender: str, message_id: str) -> bool:
        session, _ = await self._get_raw_session(sender)
        if session is None:
            return False
        return self.processed_message_id(message_id) in session.inflight_message_ids

    async def release_message_processing(self, sender: str, message_id: str) -> None:
        """Release a claim when a known pre-commit concurrency conflict is retryable."""

        digest = self.processed_message_id(message_id)
        await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "inflight_message_ids": [
                        value
                        for value in session.inflight_message_ids[-1000:]
                        if value != digest
                    ],
                    "updated_at": datetime.now(UTC),
                }
            ),
        )
        self._deactivate_turn_claim(self.thread_id(sender), digest)

    async def mark_processed(self, sender: str, message_id: str) -> None:
        digest = self.processed_message_id(message_id)

        await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "processed_message_ids": [
                        *[
                            value
                            if re.fullmatch(r"[0-9a-f]{64}", value)
                            else self.processed_message_id(value)
                            for value in session.processed_message_ids[-999:]
                        ],
                        digest,
                    ],
                    "inflight_message_ids": [
                        value
                        for value in session.inflight_message_ids[-999:]
                        if value != digest
                    ],
                    "failure_notified_message_ids": [
                        value
                        for value in session.failure_notified_message_ids[-999:]
                        if value != digest
                    ],
                    "updated_at": datetime.now(UTC),
                }
            ),
        )
        self._deactivate_turn_claim(self.thread_id(sender), digest)

    async def has_failure_notification(self, sender: str, message_id: str) -> bool:
        session, _ = await self._get_raw_session(sender)
        if session is None:
            return False
        digest = self.processed_message_id(message_id)
        return digest in session.failure_notified_message_ids

    async def mark_failure_notified(self, sender: str, message_id: str) -> None:
        digest = self.processed_message_id(message_id)
        await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "failure_notified_message_ids": [
                        *session.failure_notified_message_ids[-999:],
                        digest,
                    ],
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    async def analyze_document(
        self,
        *,
        sender: str,
        filename: str,
        content: bytes,
    ) -> LicenseEstate:
        thread_id = self.thread_id(sender)
        estate = await self._analyzer.analyze(
            thread_id=thread_id,
            filename=filename,
            content=content,
        )

        def update(session: WorkflowSession) -> WorkflowSession:
            self._ensure_new_analysis_allowed(session)
            return session.model_copy(
                update={
                    "estate": estate,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "requirement_confirmed": False,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
                    "pending_match_prompt_suspended": False,
                    "capture_messages": [],
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if estate.pending_lines
                        else WorkflowStage.AWAITING_SCENARIO
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return estate

    async def analyze_extracted(
        self,
        *,
        sender: str,
        source_file: str,
        rows: list[ParsedLicenseRow],
        seller_details: list[SellerProvidedDetail] | None = None,
        consume_capture_messages: bool = False,
    ) -> LicenseEstate:
        thread_id = self.thread_id(sender)
        estate = await self._analyzer.analyze_parsed(
            thread_id=thread_id,
            source_file=source_file,
            parsed=rows,
            seller_details=seller_details,
        )

        def update(session: WorkflowSession) -> WorkflowSession:
            self._ensure_new_analysis_allowed(
                session,
                allow_capture_messages=consume_capture_messages,
            )
            return session.model_copy(
                update={
                    "estate": estate,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "requirement_confirmed": False,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
                    "pending_match_prompt_suspended": False,
                    "capture_messages": [],
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if estate.pending_lines
                        else WorkflowStage.AWAITING_SCENARIO
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return estate

    async def append_document(
        self,
        *,
        sender: str,
        filename: str,
        content: bytes,
    ) -> LicenseEstate:
        """Add document lines to the unconfirmed requirement draft."""

        incoming = await self._analyzer.analyze(
            thread_id=self.thread_id(sender),
            filename=filename,
            content=content,
        )
        return await self._append_estate(sender, incoming)

    async def append_extracted(
        self,
        *,
        sender: str,
        source_file: str,
        rows: list[ParsedLicenseRow],
        seller_details: list[SellerProvidedDetail] | None = None,
        consume_capture_messages: bool = False,
    ) -> LicenseEstate:
        """Add multimodal/text-extracted lines to the unconfirmed draft."""

        incoming = await self._analyzer.analyze_parsed(
            thread_id=self.thread_id(sender),
            source_file=source_file,
            parsed=rows,
            seller_details=seller_details,
        )
        return await self._append_estate(
            sender,
            incoming,
            consume_capture_messages=consume_capture_messages,
        )

    async def _append_estate(
        self,
        sender: str,
        incoming: LicenseEstate,
        *,
        consume_capture_messages: bool = False,
    ) -> LicenseEstate:
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            current = self._editable_requirement(session)
            if session.confirmed_as_is is not None:
                raise ScenarioError(
                    "The full requirement is already confirmed. Should this licence be "
                    "added to the revised configuration instead?"
                )
            self._ensure_no_unfinished_work(
                session,
                action="adding another attachment",
                allow_pending_matches=True,
                allow_capture_messages=consume_capture_messages,
            )

            lines = list(current.lines)
            numeric_ids = [
                int(line.line_id[1:])
                for line in lines
                if line.line_id.startswith("L") and line.line_id[1:].isdigit()
            ]
            next_id = max(numeric_ids, default=0) + 1
            for incoming_line in incoming.lines:
                merge_index = next(
                    (
                        index
                        for index, existing in enumerate(lines)
                        if existing.match_method != "unresolved"
                        and incoming_line.match_method != "unresolved"
                        and self._same_catalogue_line_identity(
                            existing,
                            incoming_line,
                        )
                        and existing.term_duration == incoming_line.term_duration
                        and existing.billing_plan == incoming_line.billing_plan
                        and existing.expiration_date == incoming_line.expiration_date
                        and existing.renewal_date == incoming_line.renewal_date
                    ),
                    None,
                )
                if merge_index is not None:
                    existing = lines[merge_index]
                    lines[merge_index] = existing.model_copy(
                        update={
                            "total_licenses": (
                                existing.total_licenses + incoming_line.total_licenses
                            ),
                            "expired_licenses": (
                                existing.expired_licenses
                                + incoming_line.expired_licenses
                            ),
                            "assigned_licenses": (
                                existing.assigned_licenses
                                + incoming_line.assigned_licenses
                            ),
                            "renewal_quantity": (
                                existing.renewal_quantity
                                + incoming_line.renewal_quantity
                            ),
                        }
                    )
                    continue
                lines.append(
                    incoming_line.model_copy(
                        update={
                            "line_id": f"L{next_id}",
                            "row_number": max(
                                (line.row_number for line in lines),
                                default=1,
                            )
                            + 1,
                        }
                    )
                )
                next_id += 1

            details = {
                item.label.casefold(): item for item in current.seller_details
            }
            for item in incoming.seller_details:
                details[item.label.casefold()] = item
            pending = any(line.match_method == "unresolved" for line in lines)
            now = datetime.now(UTC)
            result = current.model_copy(
                update={
                    "status": (
                        EstateStatus.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else EstateStatus.READY
                    ),
                    "lines": lines,
                    "rate_card_version": incoming.rate_card_version,
                    "seller_details": list(details.values())[:12],
                    "updated_at": now,
                }
            )
            return session.model_copy(
                update={
                    "estate": result,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "requirement_confirmed": False,
                    "capture_messages": (
                        [] if consume_capture_messages else session.capture_messages
                    ),
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else WorkflowStage.AWAITING_INITIAL_VALIDATION
                    ),
                    "updated_at": now,
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def set_requirement_detail(
        self,
        sender: str,
        *,
        label: str,
        value: str,
    ) -> LicenseEstate:
        """Upsert or remove seller-supplied proposal context without inferring a value."""

        clean_label = " ".join(label.strip().split())
        clean_value = " ".join(value.strip().split())
        if not clean_label:
            raise ScenarioError("Which proposal detail would you like to add or change?")
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Provide a licensing requirement before adding details.")
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="changing proposal details",
                allow_pending_matches=True,
            )
            details = [
                item
                for item in session.estate.seller_details
                if item.label.casefold() != clean_label.casefold()
            ]
            if clean_value:
                details.append(SellerProvidedDetail(label=clean_label, value=clean_value))
            result = session.estate.model_copy(
                update={
                    "seller_details": details,
                    "updated_at": datetime.now(UTC),
                }
            )
            return session.model_copy(
                update={
                    "estate": result,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_matches(
        self,
        sender: str,
        selections: dict[str, SkuMatchSelection],
    ) -> LicenseEstate:
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ValueError("Upload a licence file before confirming SKU matches.")
            self._ensure_proposal_mutation_allowed(session)
            result = self._analyzer.confirm_matches(session.estate, selections)
            return session.model_copy(
                update={
                    "estate": result,
                    "pending_match_prompt_suspended": False,
                    "requirement_confirmed": False,
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if result.pending_lines
                        else WorkflowStage.AWAITING_SCENARIO
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def request_requirement_validation(self, sender: str) -> LicenseEstate:
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Provide a licensing requirement before validation.")
            self._ensure_proposal_mutation_allowed(session)
            if session.confirmed_as_is is not None:
                raise ScenarioError(
                    "The Renew As-Is baseline is already confirmed. Start fresh for a new "
                    "requirement, or edit the revised proposal."
                )
            self._ensure_no_unfinished_work(
                session,
                action="requesting seller validation",
            )
            result = session.estate
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_INITIAL_VALIDATION,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_requirement(self, sender: str) -> LicenseEstate:
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.stage != WorkflowStage.AWAITING_INITIAL_VALIDATION:
                raise ScenarioError("There is no requirement awaiting seller confirmation.")
            if session.estate is None or session.estate.pending_lines:
                raise ScenarioError("Resolve all SKU matches before confirming the requirement.")
            if (
                session.capture_messages
                or session.pending_dialogue is not None
                or session.pending_sku_change is not None
            ):
                raise ScenarioError(
                    "Resolve the pending requirement question before confirming the requirement."
                )
            result = session.estate
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_SCENARIO,
                    "requirement_confirmed": True,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_requirement_and_price_as_is(
        self,
        sender: str,
        *,
        promo_eligible: bool = False,
    ) -> CommercialScenario:
        """Atomically confirm a clean requirement and persist its Renew As-Is baseline.

        The seller confirmation, generated baseline, active scenario, and confirmed baseline
        are one optimistic-concurrency write. A missing price leaves the requirement at its
        validation gate, avoiding the partially confirmed state that three separate writes
        could previously create after an interruption.
        """

        catalog = await self._rate_cards.get()
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.stage != WorkflowStage.AWAITING_INITIAL_VALIDATION:
                raise ScenarioError("There is no requirement awaiting seller confirmation.")
            if session.estate is None or session.estate.pending_lines:
                raise ScenarioError("Resolve all SKU matches before confirming the requirement.")
            if (
                session.capture_messages
                or session.pending_dialogue is not None
                or session.pending_sku_change is not None
            ):
                raise ScenarioError(
                    "Resolve the pending requirement question before confirming the requirement."
                )
            result = self._scenarios.build(
                estate=session.estate,
                scenario_type=ScenarioType.RENEW_AS_IS,
                catalog=catalog,
                term_duration=self._default_term_duration,
                billing_plan=self._default_billing_plan,
                segment=self._default_segment,
                promo_eligible=promo_eligible,
            )
            if any(line.price_unavailable for line in result.lines):
                return session
            scenarios = dict(session.scenarios)
            scenarios[ScenarioType.RENEW_AS_IS] = result
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "active_scenario": ScenarioType.RENEW_AS_IS,
                    "confirmed_as_is": result.model_copy(deep=True),
                    "requirement_confirmed": True,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
                    "stage": WorkflowStage.REVIEWING_SCENARIO,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def save_confirmed_as_is(
        self,
        sender: str,
        scenario: CommercialScenario,
    ) -> CommercialScenario:
        def update(session: WorkflowSession) -> WorkflowSession:
            current = session.scenarios.get(ScenarioType.RENEW_AS_IS)
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="saving the confirmed Renew As-Is baseline",
            )
            if not session.requirement_confirmed:
                raise ScenarioError(
                    "Confirm the complete captured requirement before saving its "
                    "Renew As-Is baseline."
                )
            if session.confirmed_as_is is not None:
                if session.confirmed_as_is == scenario:
                    return session
                raise ScenarioError(
                    "The confirmed Renew As-Is baseline is already saved and cannot be "
                    "silently replaced by an edited proposal."
                )
            if (
                current is None
                or current.id != scenario.id
                or current.revision != scenario.revision
                or current != scenario
            ):
                raise ScenarioError("The confirmed as-is price is no longer current.")
            return session.model_copy(
                update={
                    "confirmed_as_is": scenario.model_copy(deep=True),
                    "stage": WorkflowStage.REVIEWING_SCENARIO,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return scenario

    async def reopen_requirement_validation(self, sender: str) -> LicenseEstate:
        """Return an unpriceable requirement to the seller-review gate."""

        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Provide a licensing requirement before reviewing it.")
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="reopening requirement validation",
            )
            result = session.estate
            return session.model_copy(
                update={
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "requirement_confirmed": False,
                    "stage": WorkflowStage.AWAITING_INITIAL_VALIDATION,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def build_scenario(
        self,
        sender: str,
        scenario_type: ScenarioType,
        *,
        base_quantity: int | None = None,
        copilot_quantity: int | None = None,
        promo_eligible: bool | None = None,
    ) -> CommercialScenario:
        catalog = await self._rate_cards.get()
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Upload a licence file before building a scenario.")
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="reviewing pricing",
            )
            if session.stage not in {
                WorkflowStage.AWAITING_SCENARIO,
                WorkflowStage.REVIEWING_SCENARIO,
            }:
                raise ScenarioError(
                    "Confirm the complete licensing requirement before reviewing pricing."
                )
            scenarios = dict(session.scenarios)
            existing = scenarios.get(scenario_type)
            if existing is not None:
                result = existing
                if base_quantity is not None and scenario_type != ScenarioType.RENEW_AS_IS:
                    result = self._scenarios.edit_quantity(result, "BASE", base_quantity)
                if (
                    copilot_quantity is not None
                    and scenario_type
                    in {ScenarioType.ME3_COPILOT, ScenarioType.ME5_COPILOT}
                ):
                    result = self._scenarios.edit_quantity(
                        result,
                        "COPILOT",
                        copilot_quantity,
                    )
                scenarios[scenario_type] = result
                return session.model_copy(
                    update={
                        "scenarios": scenarios,
                        "active_scenario": scenario_type,
                        "stage": WorkflowStage.REVIEWING_SCENARIO,
                        "updated_at": datetime.now(UTC),
                    }
                )
            inherited_promo = (
                session.scenarios[session.active_scenario].promo_eligible
                if promo_eligible is None
                and session.active_scenario is not None
                and session.active_scenario in session.scenarios
                else False
            )
            result = self._scenarios.build(
                estate=session.estate,
                scenario_type=scenario_type,
                catalog=catalog,
                term_duration=self._default_term_duration,
                billing_plan=self._default_billing_plan,
                segment=self._default_segment,
                promo_eligible=(
                    promo_eligible if promo_eligible is not None else inherited_promo
                ),
                base_quantity=base_quantity,
                copilot_quantity=copilot_quantity,
            )
            scenarios[scenario_type] = result
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "active_scenario": scenario_type,
                    "stage": WorkflowStage.REVIEWING_SCENARIO,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def request_initial_validation(self, sender: str) -> CommercialScenario:
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None or session.active_scenario is None:
                raise ScenarioError(
                    "Upload a licence file and prepare Renew As-Is before validation."
                )
            self._ensure_proposal_mutation_allowed(session)
            if session.confirmed_as_is is not None:
                raise ScenarioError(
                    "The Renew As-Is baseline is already confirmed; initial validation "
                    "cannot be restarted implicitly."
                )
            self._ensure_no_unfinished_work(
                session,
                action="requesting initial seller validation",
            )
            result = session.scenarios.get(session.active_scenario)
            if result is None:
                raise ScenarioError("The initial proposal could not be found.")
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_INITIAL_VALIDATION,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_initial_validation(self, sender: str) -> CommercialScenario:
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.stage != WorkflowStage.AWAITING_INITIAL_VALIDATION:
                raise ScenarioError("There is no initial seller validation awaiting approval.")
            self._ensure_no_unfinished_work(
                session,
                action="confirming initial seller validation",
            )
            if session.active_scenario is None:
                raise ScenarioError("The initial proposal could not be found.")
            result = session.scenarios.get(session.active_scenario)
            if result is None:
                raise ScenarioError("The initial proposal could not be found.")
            if result.unresolved_decisions or any(
                line.decision_required for line in result.lines
            ):
                raise ScenarioError(
                    "Resolve unavailable prices and seller decisions before validating "
                    "the initial analysis."
                )
            return session.model_copy(
                update={
                    "stage": WorkflowStage.REVIEWING_SCENARIO,
                    "requirement_confirmed": True,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def request_finalization(self, sender: str) -> CommercialScenario:
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.stage == WorkflowStage.FINALIZED:
                raise ScenarioError(
                    "This proposal is finalized. Start a fresh requirement before changing it."
                )
            if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
                raise ScenarioError(
                    "Final seller validation is already awaiting approval."
                )
            if session.stage != WorkflowStage.REVIEWING_SCENARIO:
                raise ScenarioError(
                    "Validate the initial estate and pricing before finalization."
                )
            if not session.requirement_confirmed:
                raise ScenarioError(
                    "Confirm the complete captured requirement before finalization."
                )
            if (
                session.pending_sku_change is not None
                or session.pending_dialogue is not None
                or session.capture_messages
                or (session.estate is not None and session.estate.pending_lines)
            ):
                raise ScenarioError(
                    "Resolve the pending licensing question or SKU selection before "
                    "finalization."
                )
            if session.active_scenario is None:
                raise ScenarioError("Select a scenario before finalizing it.")
            result = session.scenarios.get(session.active_scenario)
            if result is None:
                raise ScenarioError("The active scenario could not be found.")
            # Validate readiness without mutating or incrementing the proposal revision.
            self._scenarios.finalize(result)
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_FINAL_VALIDATION,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_finalization(self, sender: str) -> CommercialScenario:
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.stage != WorkflowStage.AWAITING_FINAL_VALIDATION:
                raise ScenarioError("There is no final seller validation awaiting approval.")
            self._ensure_no_unfinished_work(
                session,
                action="confirming final seller validation",
            )
            if session.active_scenario is None:
                raise ScenarioError("Select a scenario before finalizing it.")
            current = session.scenarios.get(session.active_scenario)
            if current is None:
                raise ScenarioError("The active scenario could not be found.")
            result = self._scenarios.finalize(current)
            scenarios = dict(session.scenarios)
            scenarios[session.active_scenario] = result
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "stage": WorkflowStage.FINALIZED,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def cancel_finalization(self, sender: str) -> bool:
        cancelled = False

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal cancelled
            cancelled = session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION
            return session.model_copy(
                update={
                    "stage": (
                        WorkflowStage.REVIEWING_SCENARIO
                        if cancelled
                        else session.stage
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return cancelled

    async def edit_quantity(
        self,
        sender: str,
        line_id: str,
        quantity: int,
    ) -> CommercialScenario:
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.edit_quantity(
                scenario, line_id.upper(), quantity
            ),
        )

    async def set_discount(
        self,
        sender: str,
        percentage: Decimal,
    ) -> CommercialScenario:
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.set_discount(scenario, percentage),
        )

    async def set_adjustment(
        self,
        sender: str,
        amount: Decimal,
    ) -> CommercialScenario:
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.set_adjustment(scenario, amount),
        )

    async def reconfigure_pricing(
        self,
        sender: str,
        *,
        term_duration: str | None = None,
        billing_plan: str | None = None,
        segment: str | None = None,
        promo_eligible: bool | None = None,
    ) -> CommercialScenario:
        catalog = await self._rate_cards.get()
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.reconfigure_pricing(
                scenario,
                catalog,
                term_duration=term_duration,
                billing_plan=billing_plan,
                segment=segment,
                promo_eligible=promo_eligible,
            ),
        )

    async def set_disposition(
        self,
        sender: str,
        line_id: str,
        disposition: MigrationDisposition,
    ) -> CommercialScenario:
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.set_disposition(
                scenario, line_id.upper(), disposition
            ),
        )

    async def add_sku(
        self,
        sender: str,
        product_query: str,
        quantity: int,
    ) -> SkuChangeResult:
        return await self._request_sku_change(
            sender=sender,
            action="add",
            product_query=product_query,
            quantity=quantity,
        )

    async def replace_sku(
        self,
        sender: str,
        line_id: str,
        product_query: str,
        quantity: int,
    ) -> SkuChangeResult:
        return await self._request_sku_change(
            sender=sender,
            action="replace",
            product_query=product_query,
            quantity=quantity,
            source_line_id=line_id.upper(),
        )

    async def confirm_sku_change(
        self,
        sender: str,
        candidate_number: int,
        *,
        confirmation_id: str | None = None,
    ) -> SkuChangeResult:
        catalog = await self._rate_cards.get()
        result: SkuChangeResult | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            self._ensure_proposal_mutation_allowed(session)
            pending = session.pending_sku_change
            if pending is None:
                raise ScenarioError("There is no pending SKU change to confirm.")
            if confirmation_id is not None and pending.id != confirmation_id:
                raise ScenarioError(
                    "That SKU confirmation is stale; submit the add/replace request again."
                )
            if pending.candidate_narrowing_required:
                raise ScenarioError(
                    "That product request is still too broad for numbered selection. "
                    "Add a product family, workload, edition, plan, or exact catalogue "
                    "ID first."
                )
            if candidate_number < 1 or candidate_number > len(pending.candidates):
                raise ScenarioError(
                    f"Choose a candidate from 1 to {len(pending.candidates)}."
                )
            candidate = pending.candidates[candidate_number - 1]
            selector = SkuSelector(
                sku_title=candidate.sku_title,
                product_id=candidate.product_id,
                sku_id=candidate.sku_id,
            )
            if pending.scope == "requirement":
                if session.estate is None:
                    raise ScenarioError("The captured requirement is no longer available.")
                if (
                    pending.requirement_fingerprint is None
                    or pending.requirement_fingerprint
                    != self._requirement_fingerprint(session.estate)
                ):
                    raise ScenarioError(
                        "The captured requirement changed after this SKU choice was shown. "
                        "Review the current requirement and submit the product change again."
                    )
                changed_estate = self._apply_requirement_sku_change(
                    session.estate,
                    pending,
                    selector,
                )
                result = SkuChangeResult(state="applied", estate=changed_estate)
                return session.model_copy(
                    update={
                        "estate": changed_estate,
                        "requirement_confirmed": False,
                        "pending_sku_change": None,
                        "stage": (
                            WorkflowStage.AWAITING_MATCH_CONFIRMATION
                            if changed_estate.pending_lines
                            else WorkflowStage.AWAITING_INITIAL_VALIDATION
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            if session.active_scenario != pending.scenario_type:
                raise ScenarioError("The active scenario changed; submit the add/replace request again.")
            if pending.scenario_type is None:
                raise ScenarioError("The pending SKU change has no scenario.")
            current = session.scenarios.get(pending.scenario_type)
            if current is None or current.revision != pending.scenario_revision:
                raise ScenarioError("The proposal changed; submit the add/replace request again.")
            changed = self._apply_sku_change(current, pending, selector, catalog)
            scenarios = dict(session.scenarios)
            scenarios[pending.scenario_type] = changed
            result = SkuChangeResult(state="applied", scenario=changed)
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "pending_sku_change": None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def cancel_sku_change(self, sender: str) -> bool:
        cancelled = False

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal cancelled
            cancelled = session.pending_sku_change is not None
            return session.model_copy(
                update={
                    "pending_sku_change": None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        return cancelled

    async def restore_pending_sku_change(
        self,
        sender: str,
        pending: PendingSkuChange,
        *,
        preserve_pending_dialogue: bool = False,
    ) -> WorkflowSession:
        """Restore an unchanged SKU choice after a read-only conversational turn.

        Help and information answers may create their own transient follow-up dialogue.
        They must not silently discard the seller's unconfirmed catalogue choice. A newer
        SKU choice always wins, and a stale choice is not restored after its requirement or
        proposal revision has changed.
        """

        def update(session: WorkflowSession) -> WorkflowSession:
            current = session.pending_sku_change
            if current is not None and current.id != pending.id:
                return session
            if session.stage in {
                WorkflowStage.AWAITING_FINAL_VALIDATION,
                WorkflowStage.FINALIZED,
            }:
                return session
            if pending.scope == "requirement":
                if (
                    session.estate is None
                    or pending.requirement_fingerprint is None
                    or pending.requirement_fingerprint
                    != self._requirement_fingerprint(session.estate)
                ):
                    return session
            else:
                if (
                    pending.scenario_type is None
                    or session.active_scenario != pending.scenario_type
                    or pending.scenario_type not in session.scenarios
                    or session.scenarios[pending.scenario_type].revision
                    != pending.scenario_revision
                ):
                    return session
            return session.model_copy(
                update={
                    "pending_sku_change": pending,
                    "pending_dialogue": (
                        session.pending_dialogue
                        if preserve_pending_dialogue
                        else None
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        return await self._mutate(sender, update)

    async def edit_requirement_quantity(
        self,
        sender: str,
        line_id: str,
        quantity: int,
    ) -> LicenseEstate:
        if quantity <= 0:
            raise ScenarioError("Requirement quantity must be greater than zero.")
        result: LicenseEstate | None = None
        normalized_id = line_id.upper()

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            estate = self._editable_requirement(session)
            self._ensure_no_unfinished_work(
                session,
                action="changing a requirement quantity",
                allow_pending_matches=True,
            )
            found = False
            lines: list[NormalizedLicenseLine] = []
            for line in estate.lines:
                if line.line_id != normalized_id:
                    lines.append(line)
                    continue
                found = True
                lines.append(
                    line.model_copy(
                        update={
                            "total_licenses": quantity,
                            "expired_licenses": 0,
                            "renewal_quantity": quantity,
                        }
                    )
                )
            if not found:
                raise ScenarioError(f"Requirement line {normalized_id!r} was not found.")
            result = estate.model_copy(
                update={"lines": lines, "updated_at": datetime.now(UTC)}
            )
            return session.model_copy(
                update={
                    "estate": result,
                    "requirement_confirmed": False,
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if result.pending_lines
                        else WorkflowStage.AWAITING_INITIAL_VALIDATION
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def remove_requirement_line(
        self,
        sender: str,
        line_id: str,
    ) -> LicenseEstate:
        return await self.remove_requirement_lines(sender, [line_id])

    async def remove_requirement_lines(
        self,
        sender: str,
        line_ids: list[str],
        *,
        expected_pending_dialogue: PendingDialogue | None = None,
    ) -> LicenseEstate:
        """Remove several original line IDs atomically without shifting the selection."""

        result: LicenseEstate | None = None
        normalized_ids = {
            line_id.strip().upper() for line_id in line_ids if line_id.strip()
        }
        if not normalized_ids:
            raise ScenarioError("Specify at least one requirement line to remove.")

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            working_session = session
            if expected_pending_dialogue is not None:
                if session.pending_dialogue != expected_pending_dialogue:
                    raise ScenarioError(
                        "The pending clarification changed before the selected lines could "
                        "be removed. Review the latest requirement and retry the removal."
                    )
                working_session = session.model_copy(
                    update={"pending_dialogue": None}
                )
            estate = self._editable_requirement(working_session)
            self._ensure_no_unfinished_work(
                working_session,
                action="removing requirement lines",
                allow_pending_matches=True,
            )
            available_ids = {line.line_id for line in estate.lines}
            missing = sorted(normalized_ids - available_ids)
            if missing:
                raise ScenarioError(
                    "Requirement line(s) not found: " + ", ".join(missing) + "."
                )
            lines = [line for line in estate.lines if line.line_id not in normalized_ids]
            if not lines:
                raise ScenarioError("A requirement must contain at least one SKU line.")
            pending = any(line.match_method == "unresolved" for line in lines)
            result = estate.model_copy(
                update={
                    "lines": lines,
                    "status": (
                        EstateStatus.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else EstateStatus.READY
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            return working_session.model_copy(
                update={
                    "estate": result,
                    "requirement_confirmed": False,
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else WorkflowStage.AWAITING_INITIAL_VALIDATION
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def edit_requirement_contract(
        self,
        sender: str,
        *,
        term_duration: str | None = None,
        billing_plan: str | None = None,
    ) -> LicenseEstate:
        if not (term_duration or billing_plan):
            raise ScenarioError("Provide a subscription term or billing plan.")
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            estate = self._editable_requirement(session)
            self._ensure_no_unfinished_work(
                session,
                action="changing requirement contract details",
                allow_pending_matches=True,
            )
            lines = [
                line.model_copy(
                    update={
                        "term_duration": term_duration or line.term_duration,
                        "billing_plan": billing_plan or line.billing_plan,
                    }
                )
                for line in estate.lines
            ]
            result = estate.model_copy(
                update={"lines": lines, "updated_at": datetime.now(UTC)}
            )
            return session.model_copy(
                update={
                    "estate": result,
                    "requirement_confirmed": False,
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if result.pending_lines
                        else WorkflowStage.AWAITING_INITIAL_VALIDATION
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def add_requirement_sku(
        self,
        sender: str,
        product_query: str,
        quantity: int,
    ) -> SkuChangeResult:
        return await self._request_requirement_sku_change(
            sender=sender,
            action="add",
            product_query=product_query,
            quantity=quantity,
        )

    async def replace_requirement_sku(
        self,
        sender: str,
        line_id: str,
        product_query: str,
        quantity: int,
    ) -> SkuChangeResult:
        return await self._request_requirement_sku_change(
            sender=sender,
            action="replace",
            product_query=product_query,
            quantity=quantity,
            source_line_id=line_id.upper(),
        )

    async def _request_requirement_sku_change(
        self,
        *,
        sender: str,
        action: Literal["add", "replace"],
        product_query: str,
        quantity: int,
        source_line_id: str | None = None,
    ) -> SkuChangeResult:
        cleaned_query = product_query.strip()
        if not cleaned_query:
            raise ScenarioError("Product query cannot be empty.")
        if quantity <= 0:
            raise ScenarioError("SKU quantity must be greater than zero.")
        catalog = await self._rate_cards.get()
        candidates = catalog.candidates(cleaned_query, limit=None)
        if not candidates:
            raise ScenarioError(
                f"I could not identify an applicable SKU for {cleaned_query!r}. "
                "What is the complete Microsoft product and plan name?"
            )
        result: SkuChangeResult | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            estate = self._editable_requirement(session)
            if session.capture_messages or session.pending_dialogue is not None:
                self._ensure_no_unfinished_work(
                    session,
                    action="starting a product add or replacement",
                    allow_pending_matches=True,
                )
            current_pending = session.pending_sku_change
            if current_pending is not None and not (
                current_pending.scope == "requirement"
                and current_pending.action == action
                and current_pending.source_line_id == source_line_id
            ):
                raise ScenarioError(
                    "Confirm or cancel the pending SKU choice before starting another "
                    "product change."
                )
            if action == "replace" and not any(
                line.line_id == source_line_id for line in estate.lines
            ):
                raise ScenarioError(f"Requirement line {source_line_id!r} was not found.")
            pending = PendingSkuChange(
                id=uuid4().hex,
                scope="requirement",
                action=action,
                source_line_id=source_line_id,
                product_query=cleaned_query,
                quantity=quantity,
                candidates=candidates,
                candidate_narrowing_required=requires_candidate_narrowing(candidates),
                requirement_fingerprint=self._requirement_fingerprint(estate),
            )
            exact = len(candidates) == 1 and candidates[0].confidence == 100
            if exact:
                candidate = candidates[0]
                changed = self._apply_requirement_sku_change(
                    estate,
                    pending,
                    SkuSelector(
                        sku_title=candidate.sku_title,
                        product_id=candidate.product_id,
                        sku_id=candidate.sku_id,
                    ),
                )
                result = SkuChangeResult(state="applied", estate=changed)
                return session.model_copy(
                    update={
                        "estate": changed,
                        "requirement_confirmed": False,
                        "pending_sku_change": None,
                        "pending_dialogue": None,
                        "stage": (
                            WorkflowStage.AWAITING_MATCH_CONFIRMATION
                            if changed.pending_lines
                            else WorkflowStage.AWAITING_INITIAL_VALIDATION
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
            result = SkuChangeResult(
                state="confirmation_required",
                confirmation=pending,
            )
            return session.model_copy(
                update={
                    "pending_sku_change": pending,
                    "pending_dialogue": None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    @staticmethod
    def _editable_requirement(session: WorkflowSession) -> LicenseEstate:
        if session.estate is None:
            raise ScenarioError("Provide a licensing requirement before editing it.")
        if session.stage not in {
            WorkflowStage.AWAITING_INITIAL_VALIDATION,
            WorkflowStage.AWAITING_MATCH_CONFIRMATION,
            WorkflowStage.AWAITING_SCENARIO,
        }:
            raise ScenarioError(
                "Requirement capture is already confirmed. Apply changes to the revised "
                "configuration instead."
            )
        if session.confirmed_as_is is not None:
            raise ScenarioError(
                "The requirement already has a confirmed Renew As-Is baseline. Apply the "
                "change to the revised configuration, or explicitly start fresh."
            )
        return session.estate

    @staticmethod
    def _apply_requirement_sku_change(
        estate: LicenseEstate,
        pending: PendingSkuChange,
        selector: SkuSelector,
    ) -> LicenseEstate:
        product_id = (selector.product_id or "").strip()
        sku_id = (selector.sku_id or "").strip()
        sku_title = selector.sku_title.strip()
        if not sku_title:
            raise ScenarioError("The selected SKU does not have a catalogue product name.")
        if bool(product_id) != bool(sku_id):
            raise ScenarioError(
                "The selected SKU has only part of its ProductId/SkuId identity. "
                "Choose another maintained catalogue option."
            )
        now = datetime.now(UTC)
        if pending.action == "add":
            numeric_ids = [
                int(line.line_id[1:])
                for line in estate.lines
                if line.line_id.startswith("L") and line.line_id[1:].isdigit()
            ]
            next_id = max(numeric_ids, default=0) + 1
            next_row = max((line.row_number for line in estate.lines), default=1) + 1
            line = NormalizedLicenseLine(
                line_id=f"L{next_id}",
                row_number=next_row,
                source_product_title=sku_title,
                product_id=product_id or None,
                sku_id=sku_id or None,
                sku_title=sku_title,
                total_licenses=pending.quantity,
                expired_licenses=0,
                assigned_licenses=0,
                renewal_quantity=pending.quantity,
                match_confidence=100,
                match_method="seller_confirmed",
            )
            return estate.model_copy(
                update={"lines": [*estate.lines, line], "updated_at": now}
            )
        if pending.source_line_id is None:
            raise ScenarioError("A replacement requires a source line ID.")
        found = False
        lines: list[NormalizedLicenseLine] = []
        for line in estate.lines:
            if line.line_id != pending.source_line_id:
                lines.append(line)
                continue
            found = True
            lines.append(
                line.model_copy(
                    update={
                        "source_product_title": sku_title,
                        "product_id": product_id or None,
                        "sku_id": sku_id or None,
                        "sku_title": sku_title,
                        "total_licenses": pending.quantity,
                        "expired_licenses": 0,
                        "renewal_quantity": pending.quantity,
                        "match_confidence": 100,
                        "match_method": "seller_confirmed",
                        "candidates": [],
                        "candidate_narrowing_required": False,
                    }
                )
            )
        if not found:
            raise ScenarioError(f"Requirement line {pending.source_line_id!r} was not found.")
        return estate.model_copy(update={"lines": lines, "updated_at": now})

    async def _request_sku_change(
        self,
        *,
        sender: str,
        action: Literal["add", "replace"],
        product_query: str,
        quantity: int,
        source_line_id: str | None = None,
    ) -> SkuChangeResult:
        cleaned_query = product_query.strip()
        if not cleaned_query:
            raise ScenarioError("Product query cannot be empty.")
        if quantity <= 0:
            raise ScenarioError("SKU quantity must be greater than zero.")
        catalog = await self._rate_cards.get()
        candidates = catalog.candidates(cleaned_query, limit=None)
        if not candidates:
            raise ScenarioError(
                f"I could not identify an applicable SKU for {cleaned_query!r}. "
                "What is the complete Microsoft product and plan name?"
            )
        result: SkuChangeResult | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            self._ensure_proposal_mutation_allowed(session)
            if session.capture_messages or session.pending_dialogue is not None:
                self._ensure_no_unfinished_work(
                    session,
                    action="starting a product add or replacement",
                )
            current_pending = session.pending_sku_change
            if current_pending is not None and not (
                current_pending.scope == "scenario"
                and current_pending.action == action
                and current_pending.source_line_id == source_line_id
                and current_pending.scenario_type == session.active_scenario
            ):
                raise ScenarioError(
                    "Confirm or cancel the pending SKU choice before starting another "
                    "product change."
                )
            if session.active_scenario is None:
                raise ScenarioError("Select a scenario before editing it.")
            current = session.scenarios.get(session.active_scenario)
            if current is None:
                raise ScenarioError("The active scenario could not be found.")
            if action == "replace" and not any(
                line.line_id == source_line_id for line in current.lines
            ):
                raise ScenarioError(
                    f"Scenario line {source_line_id!r} was not found."
                )
            exact = len(candidates) == 1 and candidates[0].confidence == 100
            if exact:
                candidate = candidates[0]
                pending = PendingSkuChange(
                    id=uuid4().hex,
                    action=action,
                    scenario_type=session.active_scenario,
                    scenario_revision=current.revision,
                    source_line_id=source_line_id,
                    product_query=cleaned_query,
                    quantity=quantity,
                    candidates=candidates,
                    candidate_narrowing_required=requires_candidate_narrowing(candidates),
                )
                selector = SkuSelector(
                    sku_title=candidate.sku_title,
                    product_id=candidate.product_id,
                    sku_id=candidate.sku_id,
                )
                changed = self._apply_sku_change(current, pending, selector, catalog)
                scenarios = dict(session.scenarios)
                scenarios[session.active_scenario] = changed
                result = SkuChangeResult(state="applied", scenario=changed)
                return session.model_copy(
                    update={
                        "scenarios": scenarios,
                        "pending_sku_change": None,
                        "pending_dialogue": None,
                        "updated_at": datetime.now(UTC),
                    }
                )

            pending = PendingSkuChange(
                id=uuid4().hex,
                action=action,
                scenario_type=session.active_scenario,
                scenario_revision=current.revision,
                source_line_id=source_line_id,
                product_query=cleaned_query,
                quantity=quantity,
                candidates=candidates,
                candidate_narrowing_required=requires_candidate_narrowing(candidates),
            )
            result = SkuChangeResult(
                state="confirmation_required",
                confirmation=pending,
            )
            return session.model_copy(
                update={
                    "pending_sku_change": pending,
                    "pending_dialogue": None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def recommend_higher_tier(
        self,
        sender: str,
        *,
        line_id: str | None = None,
        quantity: int | None = None,
    ) -> SkuChangeResult:
        """Offer same-family higher tiers without applying a migration assumption."""

        catalog = await self._rate_cards.get()
        result: SkuChangeResult | None = None
        normalized_line_id = line_id.strip().upper() if line_id else None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="starting another recommendation",
            )
            if session.confirmed_as_is is None:
                raise ScenarioError(
                    "Confirm the current requirement and Renew As-Is cost before requesting "
                    "an alternative."
                )
            if session.active_scenario is None:
                raise ScenarioError("Which proposal should I use for the recommendation?")
            current = session.scenarios.get(session.active_scenario)
            if current is None:
                raise ScenarioError("The selected proposal is no longer available.")
            eligible_lines = [
                line
                for line in current.lines
                if line.proposed_quantity > 0 and line.source_line_id is not None
            ]
            if normalized_line_id:
                source = next(
                    (line for line in eligible_lines if line.line_id == normalized_line_id),
                    None,
                )
                if source is None:
                    raise ScenarioError(
                        f"Which existing line should I evaluate? {normalized_line_id} was not found."
                    )
            elif len(eligible_lines) == 1:
                source = eligible_lines[0]
            else:
                choices = ", ".join(
                    f"{line.line_id} ({line.sku_title})" for line in eligible_lines[:8]
                )
                raise ScenarioError(
                    "Which existing line should I evaluate? " + choices
                )
            candidates = catalog.higher_tier_candidates(source.sku_title, limit=3)
            if not candidates:
                raise ScenarioError(
                    f"I found no clearly higher-tier SKU in the same product family as "
                    f"{source.sku_title}. What capability or target product should I evaluate?"
                )
            target_quantity = quantity if quantity is not None else source.proposed_quantity
            if target_quantity <= 0:
                raise ScenarioError("How many users should the recommendation cover?")
            pending = PendingSkuChange(
                id=uuid4().hex,
                action="replace",
                scenario_type=session.active_scenario,
                scenario_revision=current.revision,
                source_line_id=source.line_id,
                product_query=f"higher-tier option for {source.sku_title}",
                quantity=target_quantity,
                candidates=candidates,
            )
            result = SkuChangeResult(
                state="confirmation_required",
                confirmation=pending,
            )
            return session.model_copy(
                update={
                    "pending_sku_change": pending,
                    "pending_dialogue": None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    def _apply_sku_change(
        self,
        scenario: CommercialScenario,
        pending: PendingSkuChange,
        selector: SkuSelector,
        catalog: RateCardCatalog,
    ) -> CommercialScenario:
        if pending.action == "add":
            return self._scenarios.add_sku(
                scenario,
                product_query=pending.product_query,
                quantity=pending.quantity,
                catalog=catalog,
                selector=selector,
            )
        if pending.source_line_id is None:
            raise ScenarioError("A replacement requires a source line ID.")
        return self._scenarios.replace_sku(
            scenario,
            line_id=pending.source_line_id,
            product_query=selector.sku_title,
            quantity=pending.quantity,
            catalog=catalog,
            selector=selector,
        )

    async def add_comment(self, sender: str, comment: str) -> CommercialScenario:
        return await self._edit_active(
            sender,
            lambda scenario: self._scenarios.add_comment(scenario, comment),
        )

    async def finalize(self, sender: str) -> CommercialScenario:
        raise ScenarioError(
            "Finalization requires seller validation. Request finalization first, then "
            "confirm the displayed final validation summary."
        )

    async def simple_review(
        self,
        sender: str,
    ) -> tuple[LicenseEstate, CommercialScenario, CommercialScenario]:
        session = await self.get_session(sender)
        if session is None or session.estate is None:
            raise ScenarioError("Provide and confirm a licensing requirement first.")
        if session.confirmed_as_is is None:
            raise ScenarioError("Confirm the captured requirement before reviewing pricing.")
        if session.active_scenario is None:
            raise ScenarioError("The revised configuration could not be found.")
        revised = session.scenarios.get(session.active_scenario)
        if revised is None:
            raise ScenarioError("The revised configuration could not be found.")
        return session.estate, session.confirmed_as_is, revised

    async def comparison(
        self,
        sender: str,
    ) -> tuple[LicenseEstate, list[CommercialScenario], CommercialComparison]:
        catalog = await self._rate_cards.get()
        result: tuple[
            LicenseEstate,
            list[CommercialScenario],
            CommercialComparison,
        ] | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Upload a licence file before requesting a comparison.")
            self._ensure_proposal_mutation_allowed(session)
            if session.stage != WorkflowStage.REVIEWING_SCENARIO:
                raise ScenarioError(
                    "Confirm the complete licensing requirement and Renew As-Is price before "
                    "requesting a comparison."
                )
            if session.confirmed_as_is is None:
                raise ScenarioError(
                    "Confirm the Renew As-Is proposal before requesting a comparison."
                )
            if not session.requirement_confirmed:
                raise ScenarioError(
                    "Confirm the complete captured requirement before requesting a comparison."
                )
            if (
                session.estate.pending_lines
                or session.capture_messages
                or session.pending_sku_change is not None
                or session.pending_dialogue is not None
            ):
                raise ScenarioError(
                    "Resolve the unfinished product, quantity, or SKU selection before "
                    "requesting a comparison."
                )
            scenarios = dict(session.scenarios)
            for scenario_type in ScenarioType:
                if scenario_type not in scenarios:
                    scenario = self._scenarios.build(
                        estate=session.estate,
                        scenario_type=scenario_type,
                        catalog=catalog,
                        term_duration=self._default_term_duration,
                        billing_plan=self._default_billing_plan,
                        segment=self._default_segment,
                        promo_eligible=False,
                    )
                    scenarios[scenario_type] = scenario
            ordered = [scenarios[scenario_type] for scenario_type in ScenarioType]
            result = (
                session.estate,
                ordered,
                self._scenarios.comparison(session.thread_id, ordered),
            )
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def _edit_active(
        self,
        sender: str,
        operation: Callable[[CommercialScenario], CommercialScenario],
    ) -> CommercialScenario:
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            self._ensure_proposal_mutation_allowed(session)
            self._ensure_no_unfinished_work(
                session,
                action="editing the active proposal",
            )
            if session.active_scenario is None:
                raise ScenarioError("Select a scenario before editing it.")
            current = session.scenarios.get(session.active_scenario)
            if current is None:
                raise ScenarioError("The active scenario could not be found.")
            result = operation(current)
            scenarios = dict(session.scenarios)
            scenarios[session.active_scenario] = result
            return session.model_copy(
                update={
                    "scenarios": scenarios,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def _mutate(
        self,
        sender: str,
        operation: Callable[[WorkflowSession], WorkflowSession],
    ) -> WorkflowSession:
        thread_id = self.thread_id(sender)
        for _ in range(3):
            if self._has_active_turn_claim(thread_id):
                session, version = await self._get_raw_session(sender)
            else:
                session, version = await self._store.get(thread_id)
            if session is None:
                session = WorkflowSession(
                    id=thread_id,
                    thread_id=thread_id,
                    sender=thread_id,
                )
            updated = operation(session).model_copy(update={"sender": thread_id})
            try:
                await self._store.save(updated, version)
                return updated
            except WorkflowConflictError:
                continue
        raise WorkflowConflictError(
            "The workflow changed concurrently; retry the last operation."
        )
