from __future__ import annotations

import json
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import WorkflowSession


AgentAction = Literal[
    "help",
    "acknowledge",
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


class OfficialProductAnswer(BaseModel):
    """Read-only product guidance grounded in Microsoft-owned documentation."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    clarification_question: str
    table_title: str
    table_headers: list[str]
    table_rows: list[list[str]]
    source_urls: list[str]

    def validated(self) -> "OfficialProductAnswer":
        has_table = bool(self.table_headers or self.table_rows or self.table_title.strip())
        if not self.answer.strip() and not self.clarification_question.strip() and not has_table:
            raise IntentInterpretationError(
                "The official product answer contained no usable response."
            )
        if has_table:
            if not (2 <= len(self.table_headers) <= 5):
                raise IntentInterpretationError(
                    "The product comparison table must contain two to five columns."
                )
            if not self.table_rows or len(self.table_rows) > 30:
                raise IntentInterpretationError(
                    "The product comparison table must contain one to thirty rows."
                )
            if any(len(row) != len(self.table_headers) for row in self.table_rows):
                raise IntentInterpretationError(
                    "The product comparison table contains inconsistent row widths."
                )
        for value in self.source_urls:
            host = (urlsplit(value).hostname or "").casefold()
            if host != "microsoft.com" and not host.endswith(".microsoft.com"):
                raise IntentInterpretationError(
                    "The product answer contained a non-Microsoft source."
                )
        if self.answer.strip() and not self.source_urls:
            raise IntentInterpretationError(
                "The product answer did not contain an official Microsoft source."
            )
        return self


class RecommendationAdvisor(Protocol):
    async def advise(
        self,
        *,
        seller_request: str,
        current_sku: str,
        quantity: int,
        candidate_skus: list[str],
    ) -> OfficialRecommendation: ...

    async def answer_product_question(
        self,
        *,
        seller_question: str,
        product_names: list[str],
        proposal_context: str,
    ) -> OfficialProductAnswer: ...

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


OFFICIAL_PRODUCT_ANSWER_PROMPT = """You are a concise Microsoft licensing product advisor.
Use web search and only official Microsoft-owned documentation to answer the seller's question
about the product names supplied by the application.

Rules:
- Answer the question directly in no more than four short sentences. Compare named products only
  when the official evidence supports the comparison.
- Product names explicitly written in seller_question are authoritative for that turn and take
  precedence over older proposal products supplied as context. Never silently substitute a
  different product list.
- When the seller asks for a table, columns, a neat format, an item-by-item list, or a comparison
  covering multiple products, return the comparison in table_title, table_headers, and table_rows.
  Use two to five short columns and no more than thirty rows. Do not place a Markdown table inside
  answer. When no table is needed, return an empty title, empty headers, and empty rows.
- Distinguish product features from the customer's purchased quantities and from historical sales.
- Do not invent inventory or stock status, purchase counts, warranty terms, refunds, cancellation
  rights, future prices, eligibility, entitlements, or customer-specific contract terms.
- If the answer depends on a contract, tenant configuration, plan variant, geography, or an
  unspecified product, say so and ask one direct clarification question only when that missing
  fact prevents a useful answer. Do not ask again for a fact already supplied by the seller.
- Never calculate or quote price, discounts, promotions, margins, or commercial adjustments.
- source_urls must contain only the official Microsoft pages actually supporting the answer.
- Do not place URLs, citations, markdown links, or source names inside answer or
  clarification_question. The application retains sources for audit but does not expose them in
  the seller conversation.
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

    async def answer_product_question(
        self,
        *,
        seller_question: str,
        product_names: list[str],
        proposal_context: str,
    ) -> OfficialProductAnswer:
        if not product_names:
            raise IntentInterpretationError(
                "No product names are available for official evaluation."
            )
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=OFFICIAL_PRODUCT_ANSWER_PROMPT,
                input=json.dumps(
                    {
                        "seller_question": seller_question,
                        "product_names": product_names[:20],
                        "proposal_context": proposal_context,
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
                text_format=OfficialProductAnswer,
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=1400,
                store=False,
            )
            if getattr(response, "status", None) == "incomplete":
                raise IntentInterpretationError(
                    "The official product answer was incomplete."
                )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise IntentInterpretationError(
                    "The official product answer was empty."
                )
            return OfficialProductAnswer.model_validate(parsed).validated()
        except IntentInterpretationError:
            raise
        except (OpenAIError, ValidationError, AttributeError, TypeError, ValueError) as error:
            raise IntentInterpretationError(
                "Official Microsoft product research is temporarily unavailable."
            ) from error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()


SYSTEM_PROMPT = """You are the intent router for an SP/SSP Microsoft licensing agent.
Convert the seller's message into exactly one structured action. You never calculate prices,
select SKUs, invent migration rules, or claim an action succeeded. The application performs
and validates every commercial operation after you return the action.

Rules:
- First interpret the seller's latest message in the supplied workflow context. Classify the
  meaning of the complete turn; do not force it into the answer expected by a pending question.
- Use help for a greeting, a request to start, or a broad question such as "what do you do?".
  Write a fresh, context-aware response in response_text; do not return a memorized menu or
  command list. For a new conversation, introduce the SkySecure Microsoft Licensing Advisor,
  explain only the capabilities relevant to the seller's wording, and end with one natural
  question inviting the seller to provide a licensing requirement or business need. For a broad
  capability question, answer what the agent can do using the authoritative workflow facts below
  and then ask how it can help. If estate_uploaded is true, acknowledge the current saved draft
  using the supplied line and quantity facts; for a greeting or request to start, ask whether the
  seller wants to resume it or start fresh. Do not claim that any state changed.
- Use acknowledge for a standalone courtesy, thanks, acknowledgement, or conversational close
  that does not request a licensing action. Put one natural, context-aware sentence in
  response_text. Do not use acknowledge for "yes", "okay", or similar wording when it answers
  an active confirmation or clarification question; resolve that pending question instead.
- Use reset_requirement only when the seller explicitly asks to discard the current work and
  start fresh, reset, clear everything, or begin a new requirement. A normal greeting or the word
  "start" by itself is help, not a reset.
- When no estate is loaded, use capture_requirement if the seller mentions licence/SKU
  requirement data, even when a quantity or term is missing. The extraction step will ask for
  missing requirement details. Never treat a greeting or question as licence data.
- capture_messages contains the seller's preceding fragments for one incomplete typed
  requirement. When it is non-empty, combine the new message with those facts: never ask again
  for a product or quantity that is already present there. A new question, greeting, cancellation,
  or unrelated message interrupts that capture; classify the new turn on its own and do not force
  it into the missing product/quantity slot. Preserve the unfinished licensing facts so the seller
  can return to them later.
- When capture_messages already contains a product and the latest reply is only a quantity such
  as "10", "15 licences", or "around 20", use capture_requirement. Never use set_quantity for a
  quantity-only reply; set_quantity requires the seller to identify an existing captured line or
  product that is being changed.
- pending_dialogue contains the exact question the advisor most recently asked. Interpret a
  short reply such as a number, "yes", or "that one" only as the answer to that question. Never
  use such a reply to confirm the whole requirement while a pending dialogue exists. If the seller
  instead gives a complete new instruction, correction, question, cancellation, or out-of-scope
  message, classify that new intent rather than repeatedly demanding the old answer.
- While the stage is awaiting_initial_validation or awaiting_match_confirmation, use
  capture_requirement when the seller supplies one or more additional licence lines without
  referring to an existing line. The application appends them to the unconfirmed draft; it
  must not replace or price the existing list. Use set_quantity/replace_sku only when the
  seller explicitly refers to an existing line or correction.
- In those unconfirmed stages, "I want ME3 licence", "add ME3 within that", and equivalent
  wording are additional requirement capture. Do not use build_scenario until the seller has
  confirmed the complete requirement and Renew As-Is has been calculated.
- Use answer_question for a specific question about supported inputs, the workflow, or the
  current proposal. Put a direct, professional answer of no more than three short sentences in
  response_text. Copy quantities and commercial values exactly from the supplied context;
  never calculate, estimate, or invent them. The context includes separately named captured,
  confirmed Renew As-Is, and active-proposal line lists. Resolve words such as "these", "those",
  "the five", "that list", and "the image" to the most recent applicable line list instead of
  asking the seller to repeat product names already present in context.
- For a question requiring Microsoft product documentation--feature inclusion, storage,
  capabilities, service-plan differences, free editions, or whether a named workload such as
  Teams or Excel is included--use answer_question with detail_label
  "official_product_question", copy the seller's question into detail_value, put any explicitly
  named product in product_query, and leave response_text empty. The application will perform
  read-only research against official Microsoft sources. This is a question, not a request to add
  or replace a SKU.
- For a catalogue-wide question asking what can be purchased within a stated annual per-licence
  budget, use answer_question with detail_label "catalog_budget", put the exact positive budget
  in amount, and put an optional named product family in product_query. If the seller says
  "among these" or otherwise refers to proposal lines, answer from the supplied exact proposal
  price facts instead of searching the wider catalogue.
- Questions about cheapest, costliest, alphabetical order, line quantities, line prices, totals,
  included/removed lines, or the current product list are proposal-fact questions. Answer them
  directly from the supplied deterministic context. Unit-price extrema and alphabetized product
  names are precomputed there; do not perform new arithmetic.
- A requested licence quantity is not evidence of how many people historically bought a
  product. If asked about purchasers or popularity, state that purchase-history data is not
  available and, when useful, separately identify the proposal quantities.
- Cloud licence SKUs are not physical inventory. If asked about stock, explain that this workflow
  does not track stock and distinguish that from a SKU being present in the current maintained
  annual pricing data. Do not call retained/removed proposal dispositions "in stock" or "out of
  stock".
- Questions about warranty, refunds, cancellation rights, order timing, or future price changes
  must not be guessed from proposal data. Explain that the applicable agreement/provider policy
  controls the answer and ask for the agreement or route the seller to the licensing owner when
  the exact term is unavailable.
- Use out_of_scope for requests unrelated to Microsoft licensing requirement capture,
  annual commercial review, or the active proposal. Put one polite boundary sentence in
  response_text and briefly state what licensing task you can help with. Do not answer the
  unrelated request. A question about what licence a celebrity or unrelated third party bought is
  out of scope, even though it contains the word licence. Personal questions, affection, travel,
  sports, news, and general knowledge are also out of scope and must never begin requirement
  capture.
- Treat product shorthand in context. ME3, ME5, and ME7 refer to the Microsoft 365 E3, E5,
  and E7 catalogue families. A message that supplies a shorthand product and quantity is
  requirement capture; a question asking what the shorthand means is a licensing question.
  The application will search the maintained catalogue and require confirmation of the exact
  commercial SKU, so never invent or silently select a variant.
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
- Disposition changes require an internal line_id in structured output, but the seller does not
  need to know it. Resolve the line from the named product and supplied session context. If more
  than one line remains genuinely possible, ask which product they mean and list the full product
  names; never ask the seller to provide a line ID. Valid dispositions are retain, remove,
  migrate, and included.
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
- Use request_recommendation when the seller asks for a better SKU or a recommendation with the
  intent to upgrade, replace, add, or revise the proposal. Preserve an explicitly stated source
  line and user count. Do not
  invent a product or migration entitlement; the application will offer higher-tier SKUs from
  the same product family and require the seller to select one.
- An informational question such as "which of these is best", "is this worth the money", or
  "which suits a business" is not automatically a replacement request. Explain that different
  product families serve different needs, use exact proposal facts, and ask for the business
  priority only when it is genuinely required. Do not force the seller to select a line unless
  they ask to change or upgrade one.
- Treat broad licensing prompts such as "any suggestions from your end" as
  request_recommendation, not clarify or out_of_scope. Before the requirement is confirmed, put
  one direct question about the required business capability and user group in clarification;
  do not insist on pricing confirmation merely to provide SKU-selection guidance. After Renew
  As-Is is confirmed, preserve the requested source line and let the application evaluate a
  seller-requested change against that baseline.
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
- Never use set_requirement_detail for a Microsoft product name, SKU, plan, service, feature
  question, or the seller's answer naming a product during a product-information exchange.
- Fields irrelevant to the action must use "none", an empty string, -1, or -1.0.
- If the seller has not selected every unresolved uploaded line, use clarify and ask for
  the remaining choices; never guess a SKU match. If the seller says they do not know, do not
  repeat the same demand. Briefly acknowledge that, preserve the offered options, and ask for the
  product family/business need or suggest sending the invoice name or a screenshot.
- While an exact SKU choice is pending, a bare number or explicit option number answers that
  choice. A complete different product statement such as "Microsoft 365 E7 for one licence" is a
  correction to the pending line unless the seller explicitly says add/include/another. Route the
  correction as capture_requirement or replace_sku; never ignore it and replay stale choices.

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
- "Hi" -> help with a natural introduction and one context-appropriate question.
- "What can this agent do?" -> help with a concise capability answer tailored to the wording.
- "Thank you" or another standalone courtesy -> acknowledge with a short natural reply.
- "Can I upload a PDF?" -> answer_question with a concise supported answer.
- "Which of these five products is cheapest?" -> answer_question using the precomputed current
  proposal price facts.
- "Which plan has the most storage?" -> answer_question with detail_label
  official_product_question.
- "Can I use Teams in these products?" -> answer_question with detail_label
  official_product_question.
- "If I have INR 5,000, what can I buy?" -> answer_question with detail_label catalog_budget and
  amount 5000.
- "Write a marketing campaign" -> out_of_scope with a concise professional boundary.
- Questions about people, travel, sports, news, or other unrelated subjects -> out_of_scope,
  including during unfinished capture; do not answer their factual content.
- "ME7 1 qty" -> capture_requirement; it supplies a product family and quantity.
- With an unfinished "Defender Endpoint" capture, "10" -> capture_requirement; it supplies the
  missing quantity and is not a request to edit a line.
- With an unconfirmed draft, "Add ME3 within that" -> capture_requirement; it appends another
  licence line and is not a scenario request.
- "Can you give suggestions on picking a licence?" -> request_recommendation with one direct
  capability-and-user-group question in clarification.
- "What is ME7?" -> answer_question; explain that it is seller shorthand for the Microsoft 365
  E7 family and that the exact catalogue variant still requires selection.
- While an ambiguous line is pending, "Microsoft 365 E7 for one licence" ->
  capture_requirement as a correction to that pending line, not an option-number selection.
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

        def serialize_scenario(scenario: object | None) -> list[dict[str, object]]:
            if scenario is None:
                return []
            return [
                {
                    "line_id": line.line_id,
                    "title": line.sku_title,
                    "quantity": line.proposed_quantity,
                    "existing_quantity": line.existing_quantity,
                    "unit_price": str(line.unit_price),
                    "line_total": str(line.extended_price),
                    "price_unavailable": line.price_unavailable,
                    "term": line.term_duration,
                    "billing": line.billing_plan,
                    "disposition": line.disposition.value,
                    "category": line.category,
                }
                for line in scenario.lines[:80]
            ]

        captured_lines = (
            [
                {
                    "line_id": line.line_id,
                    "title": line.display_title,
                    "quantity": line.renewal_quantity,
                    "term": line.term_duration,
                    "billing": line.billing_plan,
                    "renewal_date": (
                        line.renewal_date.isoformat() if line.renewal_date else None
                    ),
                    "expiration_date": (
                        line.expiration_date.isoformat() if line.expiration_date else None
                    ),
                    "match_method": line.match_method,
                }
                for line in session.estate.lines[:80]
            ]
            if session.estate is not None
            else []
        )
        confirmed_lines = serialize_scenario(session.confirmed_as_is)
        active_lines = serialize_scenario(active)
        active_included_lines = [
            line for line in active_lines if int(line["quantity"]) > 0
        ]
        reference_lines = active_included_lines or confirmed_lines or captured_lines

        reference_scenario_lines = []
        if active is not None:
            reference_scenario_lines = [
                line for line in active.lines[:80] if line.proposed_quantity > 0
            ]
        elif session.confirmed_as_is is not None:
            reference_scenario_lines = list(session.confirmed_as_is.lines[:80])
        priced_reference = [
            line
            for line in reference_scenario_lines
            if not line.price_unavailable and line.unit_price > 0
        ]

        def priced_fact(line: object | None) -> dict[str, object] | None:
            if line is None:
                return None
            return {
                "line_id": line.line_id,
                "title": line.sku_title,
                "quantity": line.proposed_quantity,
                "annual_unit_price": str(line.unit_price),
                "line_total": str(line.extended_price),
            }

        cheapest = priced_fact(
            min(priced_reference, key=lambda line: line.unit_price)
            if priced_reference
            else None
        )
        costliest = priced_fact(
            max(priced_reference, key=lambda line: line.unit_price)
            if priced_reference
            else None
        )
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
                    "operation": session.pending_dialogue.operation,
                    "scope": session.pending_dialogue.scope,
                    "scenario_type": (
                        session.pending_dialogue.scenario_type.value
                        if session.pending_dialogue.scenario_type is not None
                        else "none"
                    ),
                    "source_line_id": session.pending_dialogue.source_line_id,
                    "product_query": session.pending_dialogue.product_query,
                    "quantity": session.pending_dialogue.quantity,
                    "copilot_quantity": session.pending_dialogue.copilot_quantity,
                    "disposition": session.pending_dialogue.disposition,
                    "detail_value": session.pending_dialogue.detail_value,
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
            "confirmed_renew_as_is_lines": confirmed_lines,
            "captured_requirement_lines": captured_lines,
            "active_proposal_lines": active_lines,
            "active_included_lines": active_included_lines,
            "reference_line_count": len(reference_lines),
            "reference_total_quantity": sum(
                int(line.get("quantity", 0)) for line in reference_lines
            ),
            "reference_products_alphabetical": sorted(
                [str(line["title"]) for line in reference_lines],
                key=str.casefold,
            ),
            "reference_cheapest_by_annual_unit_price": cheapest,
            "reference_costliest_by_annual_unit_price": costliest,
            "inventory_stock_data_available": False,
            "historical_purchase_count_available": False,
            "contract_warranty_refund_terms_available": False,
            "seller_provided_details": (
                [
                    {"label": item.label, "value": item.value}
                    for item in session.estate.seller_details
                ]
                if session.estate
                else []
            ),
            "lines": reference_lines,
            "lines_truncated": bool(
                (active and len(active.lines) > len(active_lines))
                or (session.estate and len(session.estate.lines) > len(captured_lines))
            ),
        }
