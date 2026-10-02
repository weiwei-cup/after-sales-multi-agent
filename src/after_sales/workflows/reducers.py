"""Commutative, idempotent joins preserve conflicts rather than selecting a winner."""

import copy

from after_sales.tools.evidence import canonical


def merge_results(left: list[dict], right: list[dict]) -> list[dict]:
    unique = {canonical(row): copy.deepcopy(row) for row in [*left, *right]}
    return [unique[key] for key in sorted(unique)]


def result_conflicts(results):
    grouped = {}
    for row in results:
        grouped.setdefault(row["task_id"], set()).add(canonical(row))
    return sorted(key for key, values in grouped.items() if len(values) > 1)


def merge_evidence(left: dict, right: dict) -> dict:
    items = merge_results(left.get("items", []), right.get("items", []))
    grouped = {}
    for item in items:
        key = (item["source_type"], item["source_id"], item.get("as_of_time"))
        grouped.setdefault(key, set()).add(
            canonical({"version": item["source_version"], "facts": item["facts"]})
        )
    conflicts = [
        {"source_type": k[0], "source_id": k[1], "as_of_time": k[2]}
        for k, variants in sorted(grouped.items(), key=lambda item: canonical(item[0]))
        if len(variants) > 1
    ]
    return {"items": items, "conflicts": conflicts}
