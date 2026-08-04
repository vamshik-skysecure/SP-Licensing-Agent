from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import MigrationDisposition, ScenarioType
from .rate_card import normalize_product_title


TARGET_SCENARIOS = {
    ScenarioType.ME3_COPILOT,
    ScenarioType.ME5_COPILOT,
    ScenarioType.ME7,
}
ALLOWED_SEED_DISPOSITIONS = {
    MigrationDisposition.RETAIN,
    MigrationDisposition.MIGRATE,
    MigrationDisposition.INCLUDED,
    MigrationDisposition.REMOVE,
    MigrationDisposition.NEEDS_DECISION,
}


class MigrationSeedRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title_pattern: str
    match_mode: Literal["contains", "prefix", "exact"] = "contains"
    priority: int = Field(default=100, ge=0)
    source: Literal["heuristic_unverified"]
    approved: bool = False
    rationale: str
    suggested_dispositions: dict[ScenarioType, MigrationDisposition]

    @model_validator(mode="after")
    def validate_suggestions(self) -> "MigrationSeedRule":
        if (
            not self.id.strip()
            or not self.title_pattern.strip()
            or not self.rationale.strip()
        ):
            raise ValueError("Migration seed IDs, patterns, and rationales cannot be empty.")
        if set(self.suggested_dispositions) != TARGET_SCENARIOS:
            raise ValueError("Each migration seed must define ME3, ME5, and ME7.")
        unsupported = set(self.suggested_dispositions.values()) - ALLOWED_SEED_DISPOSITIONS
        if unsupported:
            values = sorted(disposition.value for disposition in unsupported)
            raise ValueError(f"Unsupported seed dispositions: {values}")
        return self


class MigrationSeedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    description: str
    rules: list[MigrationSeedRule]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MigrationSeedDocument":
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Migration seed rule IDs must be unique.")
        return self


@dataclass(frozen=True)
class MigrationSeedMatch:
    rule_id: str
    source: str
    approved: bool
    disposition: MigrationDisposition
    rationale: str


class MigrationSeedCatalog:
    def __init__(self, document: MigrationSeedDocument) -> None:
        self.document = document

    @classmethod
    def load(cls, path: Path) -> "MigrationSeedCatalog":
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(f"Migration seed file was not found: {path}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"Migration seed file is invalid JSON: {path}") from error
        return cls(MigrationSeedDocument.model_validate(content))

    @property
    def rules(self) -> tuple[MigrationSeedRule, ...]:
        return tuple(self.document.rules)

    def match(
        self,
        title: str,
        scenario_type: ScenarioType,
    ) -> MigrationSeedMatch | None:
        if scenario_type not in TARGET_SCENARIOS:
            return None
        normalized_title = normalize_product_title(title)
        matches = [
            rule
            for rule in self.document.rules
            if self._matches(rule, normalized_title)
        ]
        if not matches:
            return None
        rule = min(
            matches,
            key=lambda item: (
                item.priority,
                -len(normalize_product_title(item.title_pattern)),
                item.id,
            ),
        )
        return MigrationSeedMatch(
            rule_id=rule.id,
            source=rule.source,
            approved=rule.approved,
            disposition=rule.suggested_dispositions[scenario_type],
            rationale=rule.rationale,
        )

    @staticmethod
    def _matches(rule: MigrationSeedRule, normalized_title: str) -> bool:
        pattern = normalize_product_title(rule.title_pattern)
        if rule.match_mode == "exact":
            return normalized_title == pattern
        if rule.match_mode == "prefix":
            return normalized_title.startswith(pattern)
        return pattern in normalized_title
