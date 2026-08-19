from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable, Literal
from uuid import uuid4

from .analysis import LicenseAnalyzer
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
from .rate_card import RateCardCatalog, RateCardProvider
from .scenarios import ScenarioEngine, ScenarioError, SkuSelector
from .store import WorkflowConflictError, WorkflowStore


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

    @staticmethod
    def thread_id(sender: str) -> str:
        digest = hashlib.sha256(sender.lstrip("+").encode("utf-8")).hexdigest()[:24]
        return f"wa-{digest}"

    async def get_session(self, sender: str) -> WorkflowSession | None:
        session, _ = await self._store.get(self.thread_id(sender))
        return session

    async def reset_expired_session(self, sender: str) -> bool:
        """Atomically replace expired state and report whether a reset occurred."""

        thread_id = self.thread_id(sender)
        for _ in range(3):
            session, version = await self._store.get(thread_id)
            if session is not None or version is None:
                return False
            fresh = WorkflowSession(
                id=thread_id,
                thread_id=thread_id,
                sender=sender,
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
            result = [*session.capture_messages, cleaned][-8:]
            return session.model_copy(
                update={
                    "capture_messages": result,
                    "pending_dialogue": None,
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
                    "pending_sku_change": None,
                    "pending_dialogue": None,
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
                    "pending_dialogue": None,
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

    async def has_processed(self, sender: str, message_id: str) -> bool:
        session = await self.get_session(sender)
        return bool(session and message_id in session.processed_message_ids)

    async def mark_processed(self, sender: str, message_id: str) -> None:
        await self._mutate(
            sender,
            lambda session: session.model_copy(
                update={
                    "processed_message_ids": [
                        *session.processed_message_ids[-999:],
                        message_id,
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
            return session.model_copy(
                update={
                    "estate": estate,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
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
    ) -> LicenseEstate:
        thread_id = self.thread_id(sender)
        estate = await self._analyzer.analyze_parsed(
            thread_id=thread_id,
            source_file=source_file,
            parsed=rows,
            seller_details=seller_details,
        )

        def update(session: WorkflowSession) -> WorkflowSession:
            return session.model_copy(
                update={
                    "estate": estate,
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "pending_sku_change": None,
                    "pending_dialogue": None,
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
    ) -> LicenseEstate:
        """Add multimodal/text-extracted lines to the unconfirmed draft."""

        incoming = await self._analyzer.analyze_parsed(
            thread_id=self.thread_id(sender),
            source_file=source_file,
            parsed=rows,
            seller_details=seller_details,
        )
        return await self._append_estate(sender, incoming)

    async def _append_estate(
        self,
        sender: str,
        incoming: LicenseEstate,
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
                        and existing.product_id == incoming_line.product_id
                        and existing.sku_id == incoming_line.sku_id
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
                    "pending_sku_change": None,
                    "pending_dialogue": None,
                    "capture_messages": [],
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else WorkflowStage.AWAITING_SCENARIO
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
            if session.stage == WorkflowStage.FINALIZED:
                raise ScenarioError(
                    "This proposal is finalized. Start a fresh requirement before changing it."
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
            next_stage = (
                WorkflowStage.REVIEWING_SCENARIO
                if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION
                else session.stage
            )
            return session.model_copy(
                update={
                    "estate": result,
                    "stage": next_stage,
                    "updated_at": datetime.now(UTC),
                }
            )

        await self._mutate(sender, update)
        assert result is not None
        return result

    async def confirm_matches(
        self,
        sender: str,
        selections: dict[str, tuple[str, str]],
    ) -> LicenseEstate:
        result: LicenseEstate | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ValueError("Upload a licence file before confirming SKU matches.")
            result = self._analyzer.confirm_matches(session.estate, selections)
            return session.model_copy(
                update={
                    "estate": result,
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
            if session.estate.pending_lines:
                raise ScenarioError("Confirm all pending SKU matches before validation.")
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
            if session.capture_messages or session.pending_dialogue is not None:
                raise ScenarioError(
                    "Resolve the pending requirement question before confirming the requirement."
                )
            result = session.estate
            return session.model_copy(
                update={
                    "stage": WorkflowStage.AWAITING_SCENARIO,
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
            if current is None or current.id != scenario.id:
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
            result = session.estate
            return session.model_copy(
                update={
                    "scenarios": {},
                    "active_scenario": None,
                    "confirmed_as_is": None,
                    "pending_sku_change": None,
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
                        "pending_sku_change": None,
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
                    "pending_sku_change": None,
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
            if session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION:
                raise ScenarioError(
                    "Validate the initial estate and pricing before finalization."
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
                    "pending_sku_change": None,
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
            pending = session.pending_sku_change
            if pending is None:
                raise ScenarioError("There is no pending SKU change to confirm.")
            if confirmation_id is not None and pending.id != confirmation_id:
                raise ScenarioError(
                    "That SKU confirmation is stale; submit the add/replace request again."
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
                changed_estate = self._apply_requirement_sku_change(
                    session.estate,
                    pending,
                    selector,
                )
                result = SkuChangeResult(state="applied", estate=changed_estate)
                return session.model_copy(
                    update={
                        "estate": changed_estate,
                        "pending_sku_change": None,
                        "stage": WorkflowStage.AWAITING_INITIAL_VALIDATION,
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
                    "pending_sku_change": None,
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
        result: LicenseEstate | None = None
        normalized_id = line_id.upper()

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            estate = self._editable_requirement(session)
            lines = [line for line in estate.lines if line.line_id != normalized_id]
            if len(lines) == len(estate.lines):
                raise ScenarioError(f"Requirement line {normalized_id!r} was not found.")
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
            return session.model_copy(
                update={
                    "estate": result,
                    "pending_sku_change": None,
                    "stage": (
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION
                        if pending
                        else WorkflowStage.AWAITING_SCENARIO
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
                    "pending_sku_change": None,
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
                        "pending_sku_change": None,
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
        return session.estate

    @staticmethod
    def _apply_requirement_sku_change(
        estate: LicenseEstate,
        pending: PendingSkuChange,
        selector: SkuSelector,
    ) -> LicenseEstate:
        if not selector.product_id or not selector.sku_id:
            raise ScenarioError("The selected SKU does not have a complete catalogue identity.")
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
                source_product_title=selector.sku_title,
                product_id=selector.product_id,
                sku_id=selector.sku_id,
                sku_title=selector.sku_title,
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
                        "source_product_title": selector.sku_title,
                        "product_id": selector.product_id,
                        "sku_id": selector.sku_id,
                        "sku_title": selector.sku_title,
                        "total_licenses": pending.quantity,
                        "expired_licenses": 0,
                        "renewal_quantity": pending.quantity,
                        "match_confidence": 100,
                        "match_method": "seller_confirmed",
                        "candidates": [],
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
            )
            result = SkuChangeResult(
                state="confirmation_required",
                confirmation=pending,
            )
            return session.model_copy(
                update={
                    "pending_sku_change": pending,
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
        return await self._edit_active(sender, self._scenarios.finalize)

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
                    "pending_sku_change": None,
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
                    "pending_sku_change": None,
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
            session, version = await self._store.get(thread_id)
            if session is None:
                session = WorkflowSession(
                    id=thread_id,
                    thread_id=thread_id,
                    sender=sender,
                )
            updated = operation(session)
            try:
                await self._store.save(updated, version)
                return updated
            except WorkflowConflictError:
                continue
        raise WorkflowConflictError(
            "The workflow changed concurrently; retry the last operation."
        )
