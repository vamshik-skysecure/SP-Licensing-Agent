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
    LicenseEstate,
    MigrationDisposition,
    PendingSkuChange,
    ScenarioType,
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
                    "pending_sku_change": None,
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
                    "stage": WorkflowStage.AWAITING_SCENARIO,
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
        promo_eligible: bool = False,
    ) -> CommercialScenario:
        catalog = await self._rate_cards.get()
        result: CommercialScenario | None = None

        def update(session: WorkflowSession) -> WorkflowSession:
            nonlocal result
            if session.estate is None:
                raise ScenarioError("Upload a licence file before building a scenario.")
            result = self._scenarios.build(
                estate=session.estate,
                scenario_type=scenario_type,
                catalog=catalog,
                term_duration=self._default_term_duration,
                billing_plan=self._default_billing_plan,
                segment=self._default_segment,
                promo_eligible=promo_eligible,
                base_quantity=base_quantity,
                copilot_quantity=copilot_quantity,
            )
            scenarios = dict(session.scenarios)
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
            if session.active_scenario != pending.scenario_type:
                raise ScenarioError("The active scenario changed; submit the add/replace request again.")
            current = session.scenarios.get(pending.scenario_type)
            if current is None or current.revision != pending.scenario_revision:
                raise ScenarioError("The proposal changed; submit the add/replace request again.")
            candidate = pending.candidates[candidate_number - 1]
            selector = SkuSelector(
                sku_title=candidate.sku_title,
                product_id=candidate.product_id,
                sku_id=candidate.sku_id,
            )
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
        candidates = catalog.candidates(cleaned_query, limit=3)
        if not candidates:
            raise ScenarioError(f"No rate-card SKU matched {cleaned_query!r}.")
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
                    scenarios[scenario_type] = self._scenarios.build(
                        estate=session.estate,
                        scenario_type=scenario_type,
                        catalog=catalog,
                        term_duration=self._default_term_duration,
                        billing_plan=self._default_billing_plan,
                        segment=self._default_segment,
                    )
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
