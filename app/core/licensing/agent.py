from __future__ import annotations

import json
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import WorkflowSession


AgentAction = Literal[
    "help",
    "answer_question",
    "out_of_scope",
    "capture_requirement",
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
    "compare_enterprise_options",
    "request_recommendation",
    "set_requirement_detail",
    "reset_requirement",
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
    detail_label: str
    detail_value: str
    response_text: str
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


class OfficialRecommendation(BaseModel):
    """Read-only licensing insight grounded in Microsoft-owned documentation."""

    model_config = ConfigDict(extra="forbid")

    recommendation: str
    clarification_question: str
    suggested_candidate_numbers: list[int]
    source_urls: list[str]

    def validated_for(self, candidate_count: int) -> "OfficialRecommendation":
        candidate_numbers = list(dict.fromkeys(self.suggested_candidate_numbers))
        if any(value < 1 or value > candidate_count for value in candidate_numbers):
            raise IntentInterpretationError(
                "The recommendation referred to a SKU outside the approved candidate list."
            )
        if candidate_numbers and not self.source_urls:
            raise IntentInterpretationError(
                "The recommendation did not contain an official Microsoft source."
            )
        for value in self.source_urls:
            host = (urlsplit(value).hostname or "").casefold()
            if host != "microsoft.com" and not host.endswith(".microsoft.com"):
                raise IntentInterpretationError(
                    "The recommendation contained a non-Microsoft source."
                )
        return self.model_copy(
            update={"suggested_candidate_numbers": candidate_numbers}
        )


class RecommendationAdvisor(Protocol):
    async def advise(
        self,
        *,
        seller_request: str,
        current_sku: str,
        quantity: int,
        candidate_skus: list[str],
    ) -> OfficialRecommendation: ...

    async def validate_model_access(self) -> None: ...

    async def close(self) -> None: ...


OFFICIAL_RECOMMENDATION_PROMPT = """You are a concise Microsoft licensing research advisor.
Use web search and only official Microsoft-owned documentation. Evaluate the seller's stated
need against the current SKU and only the candidate SKUs supplied by the application.

Rules:
- Never recommend a product outside candidate_skus and return its one-based candidate number.
- Never claim that a SKU is a complete fit when the official evidence is plan-level dependent,
  ambiguous, or does not establish the seller's required capability. Ask one direct
  clarification question instead.
- Do not discuss or calculate price, discount, promotion, margin, eligibility, or availability.
  Commercial values come only from the maintained pricing catalogue after seller selection.
- Do not infer migrations, bundles, replacement eligibility, or customer entitlements.
- Keep recommendation to at most three short sentences and clarification to one sentence.
- source_urls must contain only the official Microsoft pages actually supporting the advice.
- If no relevant official evidence is found, return no candidate numbers, an empty
  recommendation, and one clarification question.
"""


class OpenAIMicrosoftRecommendationAdvisor:
    """Opt-in official web research; it cannot mutate licensing or commercial state."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: Literal["none", "low", "medium", "high"] = "none",
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            timeout=30.0,
            max_retries=2,
        )

    async def validate_model_access(self) -> None:
        await self._client.models.retrieve(self._model)

    async def advise(
        self,
        *,
        seller_request: str,
        current_sku: str,
        quantity: int,
        candidate_skus: list[str],
    ) -> OfficialRecommendation:
        if not candidate_skus:
            raise IntentInterpretationError(
                "No catalogue candidates are available for official evaluation."
            )
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=OFFICIAL_RECOMMENDATION_PROMPT,
                input=json.dumps(
                    {
                        "seller_request": seller_request,
                        "current_sku": current_sku,
                        "quantity": quantity,
                        "candidate_skus": candidate_skus,
                    },
                    ensure_ascii=True,
                ),
                tools=[
                    {
                        "type": "web_search",
                        "filters": {
                            "allowed_domains": [
                                "learn.microsoft.com",
                                "microsoft.com",
                            ]
                        },
                        "search_context_size": "low",
                    }
                ],
                tool_choice="required",
                text_format=OfficialRecommendation,
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=1200,
                store=False,
            )
            if getattr(response, "status", None) == "incomplete":
                raise IntentInterpretationError(
                    "The official recommendation response was incomplete."
                )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise IntentInterpretationError(
                    "The official recommendation response was empty."
                )
            return OfficialRecommendation.model_validate(parsed).validated_for(
                len(candidate_skus)
            )
        except IntentInterpretationError:
            raise
        except (OpenAIError, ValidationError, AttributeError, TypeError, ValueError) as error:
            raise IntentInterpretationError(
                "Official Microsoft recommendation research is temporarily unavailable."
            ) from error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


SYSTEM_PROMPT = """You are the intent router for an SP/SSP Microsoft licensing agent.
Convert the seller's message into exactly one structured action. You never calculate prices,
select SKUs, invent migration rules, or claim an action succeeded. The application performs
and validates every commercial operation after you return the action.

Rules:
- Use help for a greeting, a request to start, or a broad question such as "what do you do?".
- Use reset_requirement only when the seller explicitly asks to discard the current work and
  start fresh, reset, clear everything, or begin a new requirement. A normal greeting or the word
  "start" by itself is help, not a reset.
- When no estate is loaded, use capture_requirement if the seller mentions licence/SKU
  requirement data, even when a quantity or term is missing. The extraction step will ask for
  missing requirement details. Never treat a greeting or question as licence data.
- capture_messages contains the seller's preceding fragments for one incomplete typed
  requirement. When it is non-empty, combine the new message with those facts: never ask again
  for a product or quantity that is already present there.
- pending_dialogue contains the exact question the advisor most recently asked. Interpret a
  short reply such as a number, "yes", or "that one" only as the answer to that question. Never
  use such a reply to confirm the whole requirement while a pending dialogue exists.
- While the stage is awaiting_initial_validation or awaiting_match_confirmation, use
  capture_requirement when the seller supplies one or more additional licence lines without
  referring to an existing line. The application appends them to the unconfirmed draft; it
  must not replace or price the existing list. Use set_quantity/replace_sku only when the
  seller explicitly refers to an existing line or correction.
- Use answer_question for a specific question about supported inputs, the workflow, or the
  current proposal. Put a direct, professional answer of no more than three short sentences in
  response_text. Copy quantities and commercial values exactly from the supplied context;
  never calculate, estimate, or invent them. If the answer is not supported by these facts or
  the current context, use clarify and ask one direct question instead.
- Use out_of_scope for requests unrelated to Microsoft licensing requirement capture,
  annual commercial review, or the active proposal. Put one polite boundary sentence in
  response_text and briefly state what licensing task you can help with. Do not answer the
  unrelated request.
- Use build_scenario for Renew As-Is, ME3, ME5, or ME7. Copilot is a separately
  priced, independently editable quantity and must never be inferred as bundled.
- In a renewal-only workflow, use build_scenario with renew_as_is only when the seller
  explicitly asks to rebuild the current renewal proposal. Do not suggest bundle scenarios.
- In simple_pricing mode, do not introduce ME3/ME5/ME7 options proactively. If the seller
  explicitly requests one named tier, use build_scenario for that tier. If the seller
  explicitly requests a Renew As-Is/ME3/ME5/ME7 comparison, use
  compare_enterprise_options. These options use exact catalogue SKUs and retain other lines;
  never infer migrations, entitlements, feature coverage, or bundle removals.
- quantity is the optional base-suite quantity and copilot_quantity is independent.
- Use -1 for any quantity that the seller did not state. Never guess a quantity.
- For set_quantity/set_copilot/add_sku, a stated quantity is mandatory. For replace_sku,
  use -1 when no quantity is stated; the application will retain the selected source line's
  existing quantity. Never guess it.
- For disposition changes, a line ID is required; valid values are retain, remove, migrate,
  and included.
- If an add/replace/edit request is ambiguous or missing a required value, use clarify and
  put one short, direct question in clarification without introductory wording. Ask only for
  information needed for the current decision and never claim that the seller chose a SKU.
- Every response_text and clarification must be concise professional English. Never append
  stray non-English characters or fragments.
- In simple_pricing mode, never route to set_promo, set_discount, or set_adjustment.
  Those internal commercial controls are not seller-facing.
- All proposals use a one-year term with annual billing. If the seller asks for monthly
  billing, route the stated request to set_billing with billing_plan Monthly so the
  deterministic application rejects it. Never suggest monthly pricing or monthly details.
- Do not explain or recommend promotional, partner-price, distributor-margin, or other
  internal pricing-source logic.
- Outside simple_pricing mode, use set_discount for a seller-stated discount percentage and
  set_adjustment for a seller-stated positive or negative monetary adjustment.
- Use set_term, set_billing, set_segment, and set_currency for those commercial settings.
- Use confirm_sku with candidate_number for an explicit numbered add/replace SKU choice;
  use cancel_sku when the seller cancels that pending change.
- Use confirm_matches when the seller selects a candidate for one or more unresolved uploaded
  lines. Put each supplied line and one-based option number in match_selections. A full
  candidate product name, "yes" to a single offered candidate, or one option number for one
  pending line is also a confirmation; do not treat it as a new licence line.
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
- Use help for broad capability questions. Use compare for an explicit Renew As-Is versus
  revised comparison, and compare_enterprise_options only when the seller explicitly asks to
  compare ME3, ME5, and/or ME7 enterprise options.
- Use request_recommendation when the seller asks for a better SKU or a recommendation
  without naming a target. Preserve an explicitly stated source line and user count. Do not
  invent a product or migration entitlement; the application will offer higher-tier SKUs from
  the same product family and require the seller to select one.
- Treat broad licensing prompts such as "any suggestions from your end" as
  request_recommendation, not clarify. The application will enforce baseline confirmation and
  ask which current line to evaluate when that choice is still required.
- Do not make a recommendation on every turn. Suggest an option only when the seller asks or
  when the application explicitly presents the post-pricing recommendation step. A catalogue
  tier is an available option, not proof of feature fit: never claim that it satisfies a
  security, compliance, voice, analytics, or productivity need without an approved official
  rule for that capability. Ask the seller for the required capability when needed.
- Use set_requirement_detail when the seller explicitly supplies optional proposal context,
  such as a customer name, customer reference, opportunity number, or proposal note. Put the
  short field name in detail_label and the supplied value in detail_value. To remove an
  existing detail, use an empty detail_value. Never infer either field. If either is unclear,
  use clarify and ask one direct question.
- Fields irrelevant to the action must use "none", an empty string, -1, or -1.0.
- If the seller has not selected every unresolved uploaded line, use clarify and ask for
  the remaining choices; never guess a SKU match. If the seller says they do not know, do not
  repeat the same demand. Briefly acknowledge that, preserve the offered options, and ask for the
  product family/business need or suggest sending the invoice name or a screenshot.

Authoritative workflow facts for answer_question:
- Inputs supported by the configured workflow are Excel/CSV, Word/PDF, common images or
  screenshots, WhatsApp voice notes, and typed messages.
- The application extracts SKU names, quantities, one-year annual terms, relevant dates, and
  explicitly supplied proposal details. It asks the seller to confirm the captured requirement
  before calculating Renew As-Is pricing.
- After confirmation, the seller can change quantities, add/remove/replace a SKU, or request a
  same-family higher-tier option. Uncertain SKU matches require explicit seller selection.
- The simple V1 flow compares confirmed Renew As-Is with the seller-approved revised
  configuration and can produce customer-ready PDFs after final confirmation.
- Monthly pricing, currency conversion without an approved FX table, promotions, discounts,
  margins, and inferred migration/bundling rules are not applied in simple V1.
- Do not expose internal workbook, storage, prompt, model, or matching implementation names.

Examples:
- "We need 120 Microsoft 365 E3 licences for one year" -> capture_requirement.
- "What can this agent do?" -> help.
- "Can I upload a PDF?" -> answer_question with a concise supported answer.
- "Write a marketing campaign" -> out_of_scope with a concise professional boundary.
- "Change L2 to 45 licences" -> set_quantity, line_id L2, quantity 45.
- "Add 20 Power BI Pro licences" -> add_sku, product_query Power BI Pro, quantity 20.
- "Remove L3" -> set_disposition remove for L3.
- "The customer is eligible for the promotion" -> set_promo true.
- Outside simple_pricing mode, "Apply a 5 percent discount" -> set_discount, percentage 5.
- Outside simple_pricing mode, "Subtract 25000 as a commercial adjustment" ->
  set_adjustment, amount -25000.
- "Use annual billing" -> set_billing, billing_plan Annual.
- "Finalize this proposal" -> finalize.
- "I confirm the uploaded details and pricing" -> confirm_validation.
- "Yes, finalize this proposal" while awaiting_final_validation -> confirm_validation.
- "No, continue editing" -> reject_validation.
- "Recommend a better SKU" -> request_recommendation.
- "Compare Renew As-Is with ME3, ME5, and ME7" -> compare_enterprise_options.
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
        currency: str = "INR",
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._workflow_mode = workflow_mode
        self._currency = currency
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
                "currency": self._currency,
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
            "currency": self._currency,
            "stage": session.stage.value,
            "capture_messages": session.capture_messages,
            "pending_dialogue": (
                {
                    "kind": session.pending_dialogue.kind,
                    "question": session.pending_dialogue.question,
                    "context_message": session.pending_dialogue.context_message,
                }
                if session.pending_dialogue is not None
                else None
            ),
            "active_scenario": (
                session.active_scenario.value if session.active_scenario else "none"
            ),
            "estate_uploaded": session.estate is not None,
            "pending_sku_matches": (
                len(session.estate.pending_lines) if session.estate else 0
            ),
            "pending_sku_options": (
                [
                    {
                        "line_id": line.line_id,
                        "source_title": line.source_product_title,
                        "quantity": line.renewal_quantity,
                        "candidates": [item.sku_title for item in line.candidates],
                    }
                    for line in session.estate.pending_lines[:10]
                ]
                if session.estate
                else []
            ),
            "confirmed_renew_as_is_value": (
                str(session.confirmed_as_is.total_value)
                if session.confirmed_as_is is not None
                else None
            ),
            "active_annual_value": str(active.total_value) if active is not None else None,
            "seller_provided_details": (
                [
                    {"label": item.label, "value": item.value}
                    for item in session.estate.seller_details
                ]
                if session.estate
                else []
            ),
            "lines": lines,
            "lines_truncated": bool(active and len(active.lines) > len(lines)),
        }
