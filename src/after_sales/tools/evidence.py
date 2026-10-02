"""Content-addressed evidence, restricted to one trusted ticket investigation."""

import hashlib
import json
from threading import RLock

from after_sales.tools.contracts import (
    ErrorCode,
    Evidence,
    EvidenceRef,
    ToolContext,
    ToolFailure,
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvidenceStore:
    """P02 keeps snapshots in memory. P06 will persist them with the workflow run."""

    def __init__(self):
        self._items: dict[str, Evidence] = {}
        self._lock = RLock()

    def prepare(
        self,
        context: ToolContext,
        source_type: str,
        source_id: str,
        source_version: str,
        facts: object,
    ) -> Evidence:
        values = {
            "ticket_id": context.ticket_id,
            "session_id": context.session_id,
            "source_type": source_type,
            "source_id": source_id,
            "source_version": source_version,
            # Deterministic demo observation clock. Real adapters must supply their read clock.
            "observed_at": context.as_of_time.isoformat(),
            "as_of_time": context.as_of_time.isoformat(),
            "facts_json": canonical(facts),
        }
        digest = hashlib.sha256(canonical(values).encode()).hexdigest()
        return Evidence(id=f"E-{digest[:40]}", **values)

    def register(self, evidence: tuple[Evidence, ...]) -> None:
        with self._lock:
            for item in evidence:
                old = self._items.get(item.id)
                if old is not None and old != item:
                    raise ValueError("evidence ID collision with different content")
            self._items.update({item.id: item for item in evidence})

    def restore_trusted(self, exported: list[dict], context: ToolContext) -> None:
        """Restore only the application's durable run store, never a CLI report file."""
        items = []
        for row in exported:
            values = {key: value for key, value in row.items() if key != "facts"}
            item = Evidence.model_validate({**values, "facts_json": canonical(row["facts"])})
            if (item.ticket_id, item.session_id) != (context.ticket_id, context.session_id):
                raise ValueError("persisted evidence must belong to the restored run")
            content = item.model_dump(mode="python", exclude={"id"})
            for key in ("observed_at", "as_of_time"):
                content[key] = content[key].isoformat()
            expected = "E-" + hashlib.sha256(canonical(content).encode()).hexdigest()[:40]
            if item.id != expected:
                raise ValueError("persisted evidence content hash mismatch")
            items.append(item)
        self.register(tuple(items))

    def resolve(
        self, ref: EvidenceRef, context: ToolContext, source_type: str | None = None
    ) -> Evidence:
        with self._lock:
            item = self._items.get(ref.evidence_id)
        if item is None:
            raise ToolFailure(ErrorCode.EVIDENCE_NOT_FOUND, "证据引用不存在")
        if item.ticket_id != context.ticket_id or item.session_id != context.session_id:
            raise ToolFailure(ErrorCode.EVIDENCE_SCOPE_MISMATCH, "证据不属于当前工单调查会话")
        if item.source_version != ref.source_version:
            raise ToolFailure(ErrorCode.EVIDENCE_VERSION_MISMATCH, "证据来源版本不符")
        if source_type is not None and item.source_type != source_type:
            raise ToolFailure(ErrorCode.EVIDENCE_SOURCE_MISMATCH, "证据来源类型不符")
        return item

    def export(self, context: ToolContext) -> list[dict[str, object]]:
        with self._lock:
            items = [
                item
                for item in self._items.values()
                if item.ticket_id == context.ticket_id and item.session_id == context.session_id
            ]
        return [
            {
                **item.model_dump(mode="json", exclude={"facts_json"}),
                "facts": json.loads(item.facts_json),
            }
            for item in sorted(items, key=lambda item: item.id)
        ]
