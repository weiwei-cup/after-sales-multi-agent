"""Versioned evaluation inputs and separate, strictly validated scoring expectations."""

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from after_sales.api.contracts import CreateTicket
from after_sales.domain.models import ActionType, Cents, DomainModel, Identifier, UtcTime

FAULTS = {
    "invalid_evidence_reference",
    "review_always_rejects",
    "repeat_same_action",
    "get_tracking_timeout_once",
    "get_delivery_proof_always_times_out",
    "order_changes_after_approval",
    "crash_after_action_commit",
    "one_model_call_remaining_two_workers",
    "repeat_start_and_confirm",
    "refresh_and_reconnect",
}


class EvaluationCase(DomainModel):
    id: Identifier
    title: str
    ticket_id: Identifier | None = None
    request: dict | None = None
    first_stage: str | None = None
    fault_injections: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_input(self) -> Self:
        if (self.ticket_id is None) == (self.request is None):
            raise ValueError("a case requires exactly one ticket or request")
        if (
            len(set(self.fault_injections)) != len(self.fault_injections)
            or set(self.fault_injections) - FAULTS
        ):
            raise ValueError("unknown or duplicate fault injection")
        if self.request:
            customer = self.request.get("customer_id")
            if not isinstance(customer, str) or not customer:
                raise ValueError("holdout request requires customer_id")
            CreateTicket.model_validate(
                {k: v for k, v in self.request.items() if k != "customer_id"}
            )
        return self


class CaseDocument(DomainModel):
    version: Literal["cases-v1"]
    dataset_version: Literal["demo-v1"]
    as_of_time: UtcTime
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate evaluation case ID")
        return self


class GoldCase(DomainModel):
    case_id: Identifier
    allowed_outcomes: tuple[str, ...] = Field(min_length=1)
    allowed_actions: tuple[ActionType, ...]
    forbidden_behaviors: tuple[str, ...]
    required_source_types: tuple[str, ...]
    requires_operator_approval: bool
    maximum_refund_cents: Cents | None = None
    maximum_repair_count: int | None = Field(default=None, ge=0)
    maximum_business_writes_per_action: int | None = Field(default=None, ge=1)


class GoldDocument(DomainModel):
    version: Literal["gold-v1"]
    dataset_version: Literal["demo-v1"]
    cases: tuple[GoldCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate gold case ID")
        return self


def read_cases(root: Path, split: str) -> tuple[CaseDocument, str]:
    path = root / (
        "fixtures/dev-cases-v1.json" if split == "dev" else "evals/holdout/cases-v1.json"
    )
    raw = path.read_bytes()
    return CaseDocument.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def read_gold(root: Path, split: str, cases: CaseDocument) -> tuple[dict[str, GoldCase], str]:
    raw = (root / f"evals/gold/{split}-v1.json").read_bytes()
    gold = GoldDocument.model_validate_json(raw)
    if gold.dataset_version != cases.dataset_version or {c.case_id for c in gold.cases} != {
        c.id for c in cases.cases
    }:
        raise ValueError("case inputs and gold must have matching versions and exact IDs")
    return {c.case_id: c for c in gold.cases}, hashlib.sha256(raw).hexdigest()


def write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")
