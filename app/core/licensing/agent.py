from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import WorkflowSession


AgentAction = Literal[
    "help",
    "build_scenario",
    "set_quantity",
    "set_copilot",
    "set_disposition",
    "add_sku",
    "replace_sku",
    "set_promo",
    "set_discount",
    "set_adjustment",
    "set_term",
    "set_billing",
    "set_segment",
    "set_currency",
    "confirm_matches",
    "confirm_sku",
    "cancel_sku",
    "add_comment",
    "finalize",
    "confirm_validation",
    "reject_validation",
    "compare",
    "request_recommendation",
    "clarify",
]


class MatchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str
    candidate_number: int


class AgentIntent(BaseModel):
    """A validated command proposed by the language model.

    Sentinel values keep every property required by structured outputs.
    They are converted to optional values only at the deterministic boundary.
    """

    model_config = ConfigDict(extra="forbid")

    action: AgentAction
    scenario: Literal[
        "renew_as_is", "me3_copilot", "me5_copilot", "me7", "none"
    ]
    line_id: str
    quantity: int
    copilot_quantity: int
    product_query: str
    disposition: Literal[
        "retain", "remove", "migrate", "included", "none"
    ]
    boolean_value: Literal["true", "false", "none"]
    percentage: float
    amount: float
    term_duration: str
    billing_plan: str
    segment: str
    currency: str
    candidate_number: int
    match_selections: list[MatchSelection]
    comment: str
    clarification: str


class IntentInterpretationError(RuntimeError):
    pass


class IntentInterpreter(Protocol):
    async def interpret(
        self,
        message: str,
        session: WorkflowSession | None,
    ) -> AgentIntent: ...

    async def close(self) -> None: ...


SYSTEM_PROMPT = """You are the intent router for an SP/SSP Microsoft licensing agent.
Convert the seller's message into exactly one structured action. You never calculate prices,
select SKUs, invent migration rules, or claim an action succeeded. The application performs
and validates every commercial operation after you return the action.

Rules:
- Use build_scenario for Renew As-Is, ME3, ME5, or ME7. Copilot is a separately
  priced, independently editable quantity and must never be inferred as bundled.
- In a renewal-only workflow, use build_scenario with renew_as_is only when the seller
  explicitly asks to rebuild the current renewal proposal. Do not suggest bundle scenarios.
- quantity is the optional base-suite quantity and copilot_quantity is independent.
- Use -1 for any quantity that the seller did not state. Never guess a quantity.
- For set_quantity/set_copilot/add_sku, a stated quantity is mandatory. For replace_sku,
  use -1 when no quantity is stated; the application will retain the selected source line's
  existing quantity. Never guess it.
- For disposition changes, a line ID is required; valid values are retain, remove, migrate,
  and included.
- If an add/replace/edit request is ambiguous or missing a required value, use clarify and
  put one short, direct question in clarification without introductory wording.
- In simple_pricing mode, never route to set_promo, set_discount, or set_adjustment.
  Those internal commercial controls are not seller-facing.
- All proposals use a one-year term with annual billing. If the seller asks for monthly
  billing, route the stated request to set_billing with billing_plan Monthly so the
  deterministic application rejects it. Never suggest monthly pricing or monthly details.
- Do not explain or recommend promotional, partner-price, distributor-margin, or other
  internal pricing-source logic.
- Use set_discount for a seller-stated discount percentage and set_adjustment for a
  seller-stated positive or negative monetary adjustment.
- Use set_term, set_billing, set_segment, and set_currency for those commercial settings.
- Use confirm_sku with candidate_number for an explicit numbered add/replace SKU choice;
  use cancel_sku when the seller cancels that pending change.
- Use confirm_matches when the seller selects numbered candidates for every unresolved
  uploaded line. Put each line and one-based option number in match_selections.
- Use add_comment only for an explicit note or assumption.
- Use finalize only when the seller explicitly asks to start finalization. The application
  will show a final validation summary and ask for a second confirmation.
- Use confirm_validation when the seller explicitly approves the displayed analysis/pricing
  during awaiting_initial_validation, or explicitly approves finalization during
  awaiting_final_validation.
- Use reject_validation when the seller reports that the displayed initial details are
  incorrect, cancels finalization, or asks to continue editing at a validation gate.
- A bare "yes" means confirm_validation only when the current stage is one of the two
  validation stages. Otherwise use clarify.
- Use help for capability questions and compare only for explicit requests.
- Use request_recommendation when the seller asks for a better SKU or a recommendation
  without naming a target. Do not choose a product. The application will ask for the
  business capability, source line, and user count because authoritative rules are pending.
- Fields irrelevant to the action must use "none", an empty string, -1, or -1.0.
- If the seller has not selected every unresolved uploaded line, use clarify and ask for
  the remaining choices; never guess a SKU match.

Examples:
- "Change L2 to 45 licences" -> set_quantity, line_id L2, quantity 45.
- "Add 20 Power BI Pro licences" -> add_sku, product_query Power BI Pro, quantity 20.
- "Remove L3" -> set_disposition remove for L3.
- "The customer is eligible for the promotion" -> set_promo true.
- "Apply a 5 percent discount" -> set_discount, percentage 5.
- "Subtract 25000 as a commercial adjustment" -> set_adjustment, amount -25000.
- "Use annual billing" -> set_billing, billing_plan Annual.
- "Finalize this proposal" -> finalize.
- "I confirm the uploaded details and pricing" -> confirm_validation.
- "Yes, finalize this proposal" while awaiting_final_validation -> confirm_validation.
- "No, continue editing" -> reject_validation.
- "Recommend a better SKU" -> request_recommendation.
"""


class OpenAIIntentInterpreter:
    """Natural-language adapter; it has no permission to mutate commercial state."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: Literal["none", "low", "medium", "high"] = "none",
        workflow_mode: str = "scenario_comparison",
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._workflow_mode = workflow_mode
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            timeout=20.0,
            max_retries=2,
        )

    async def validate_model_access(self) -> None:
        """Verify model access without running an inference."""
        await self._client.models.retrieve(self._model)

    async def interpret(
        self,
        message: str,
        session: WorkflowSession | None,
    ) -> AgentIntent:
        context = self._context(session)
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=SYSTEM_PROMPT,
                input=(
                    "Current deterministic workflow context:\n"
                    f"{json.dumps(context, ensure_ascii=True)}\n\n"
                    f"Seller message:\n{message}"
                ),
                text_format=AgentIntent,
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=1000,
                store=False,
            )
            if getattr(response, "status", None) == "incomplete":
                raise IntentInterpretationError("The intent response was incomplete.")
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise IntentInterpretationError("The intent response was empty.")
            return AgentIntent.model_validate(parsed)
        except IntentInterpretationError:
            raise
        except (OpenAIError, ValidationError, AttributeError, TypeError, ValueError) as error:
            raise IntentInterpretationError(
                "Natural-language interpretation is temporarily unavailable."
            ) from error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    def _context(self, session: WorkflowSession | None) -> dict[str, object]:
        if session is None:
            return {
                "workflow_mode": self._workflow_mode,
                "stage": "awaiting_upload",
                "active_scenario": "none",
                "lines": [],
            }
        active = (
            session.scenarios.get(session.active_scenario)
            if session.active_scenario is not None
            else None
        )
        lines = []
        if active is not None:
            lines = [
                {
                    "line_id": line.line_id,
                    "title": line.sku_title,
                    "quantity": line.proposed_quantity,
                    "disposition": line.disposition.value,
                }
                for line in active.lines[:80]
            ]
        elif session.estate is not None:
            lines = [
                {
                    "line_id": line.line_id,
                    "title": line.display_title,
                    "quantity": line.renewal_quantity,
                    "disposition": "captured_requirement",
                }
                for line in session.estate.lines[:80]
            ]
        return {
            "workflow_mode": self._workflow_mode,
            "stage": session.stage.value,
            "active_scenario": (
                session.active_scenario.value if session.active_scenario else "none"
            ),
            "estate_uploaded": session.estate is not None,
            "pending_sku_matches": (
                len(session.estate.pending_lines) if session.estate else 0
            ),
            "lines": lines,
            "lines_truncated": bool(active and len(active.lines) > len(lines)),
        }
