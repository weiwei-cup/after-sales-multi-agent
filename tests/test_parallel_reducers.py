import copy

from after_sales.workflows.reducers import merge_evidence, merge_results, result_conflicts


def test_duplicate_results_are_idempotent_commutative_and_keep_conflicts():
    first = {"task_id": "1:order", "output": {"order_version": 1}}
    second = {"task_id": "1:order", "output": {"order_version": 2}}
    policy = {"task_id": "1:policy", "output": {"found": True}}
    assert merge_results([first], [first]) == [first]
    assert merge_results([first], [second, policy]) == merge_results([second, policy], [first])
    assert merge_results(merge_results([first], [second]), [policy]) == merge_results(
        [first], merge_results([second], [policy])
    )
    joined = merge_results([first], [second])
    assert len(joined) == 2 and result_conflicts(joined) == ["1:order"]


def test_evidence_versions_and_same_version_content_conflicts_are_explicit():
    first = {
        "id": "E-1",
        "source_type": "order",
        "source_id": "ORD-004",
        "source_version": "1",
        "as_of_time": "2026-10-02T04:00:00Z",
        "facts": {"refunded_cents": 0},
    }
    newer = {**first, "id": "E-2", "source_version": "2", "facts": {"refunded_cents": 1000}}
    changed = {**first, "id": "E-3", "facts": {"refunded_cents": 1}}
    same = merge_evidence({"items": [first]}, {"items": [copy.deepcopy(first)]})
    assert same == {"items": [first], "conflicts": []}
    for conflicting in (newer, changed):
        left = merge_evidence({"items": [first]}, {"items": [conflicting]})
        assert left == merge_evidence({"items": [conflicting]}, {"items": [first]})
        assert len(left["items"]) == 2 and left["conflicts"][0]["source_id"] == "ORD-004"
    # A later deliberate refresh has a distinct observation scope.
    later = {**newer, "as_of_time": "2026-10-03T04:00:00Z"}
    assert merge_evidence({"items": [first]}, {"items": [later]})["conflicts"] == []
