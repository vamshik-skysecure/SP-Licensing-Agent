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
    "add_comment",
    "finalize",
    "compare",
    "clarify",
]


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
- Use build_scenario for Renew As-Is, ME3 + Copilot, ME5 + Copilot, or ME7.
- quantity is the optional base-suite quantity and copilot_quantity is independent.
- Use -1 for any quantity that the seller did not state. Never guess a quantity.
- For set_quantity/set_copilot/add_sku/replace_sku, a stated quantity is mandatory.
- For disposition changes, a line ID is required; valid values are retain, remove, migrate,
  and included.
- If an add/replace/edit request is ambiguous or missing a required value, use clarify and
  ask one short question in clarification.
- Use add_comment only for an explicit note or assumption.
- Use help for capability questions and compare/finalize only for explicit requests.
- Fields irrelevant to the action must use "none", an empty string, or -1.
- If SKU-match confirmation is requested, use clarify and direct the seller to the pending
  confirmation choices shown by the application.
"""


class OpenAIIntentInterpreter:
    """Natural-language adapter; it has no permission to mutate commercial state."""

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
            timeout=20.0,
            max_retries=2,
        )

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

    @staticmethod
    def _context(session: WorkflowSession | None) -> dict[str, object]:
        if session is None:
            return {"stage": "awaiting_upload", "active_scenario": "none", "lines": []}
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
        return {
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
