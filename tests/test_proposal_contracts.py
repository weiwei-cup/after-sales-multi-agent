import pytest
from pydantic import ValidationError

from after_sales.agents.contracts import ActionCandidate, ResolutionProposal

pytestmark = pytest.mark.unit


def candidate(amount=10000):
    return {
        "type": "issue_mock_refund",
        "order_id": "ORD-004",
        "amount_cents": amount,
        "assessment_ref": {"evidence_id": "E-assessment", "source_version": "rules-v1"},
        "policy_refs": [{"policy_id": "LOST-REFUND", "version": 1}],
        "evidence_refs": [{"evidence_id": "E-order", "source_version": "1"}],
    }


@pytest.mark.parametrize("amount", [-1, 0, 1.2, True])
def test_refund_candidate_money_is_strict_positive_integer(amount):
    with pytest.raises(ValidationError):
        ActionCandidate.model_validate(candidate(amount))


def test_non_refund_action_does_not_carry_a_money_amount():
    payload = {**candidate(), "type": "create_return_request"}
    with pytest.raises(ValidationError, match="only refund"):
        ActionCandidate.model_validate(payload)


def test_information_request_must_contain_an_explicit_question():
    with pytest.raises(ValidationError, match="explicit question"):
        ResolutionProposal(
            ticket_id="T-ONE", decision="request_information", customer_reply_draft="补资料"
        )


def test_decision_cannot_silently_include_an_unrelated_action():
    with pytest.raises(ValidationError, match="match"):
        ResolutionProposal(
            ticket_id="T-ONE",
            order_id="ORD-004",
            decision="propose_return",
            actions=[candidate()],
            customer_reply_draft="退货候选",
        )


def test_proposal_cannot_claim_execution_or_add_unrecognized_fields():
    for extra in ({"candidate_only": False}, {"executed": True}):
        with pytest.raises(ValidationError):
            ResolutionProposal(
                ticket_id="T-ONE",
                decision="inform_progress",
                customer_reply_draft="进度说明",
                **extra,
            )
