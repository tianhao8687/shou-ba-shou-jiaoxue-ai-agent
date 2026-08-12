from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..schemas import AuditEvent, RunRecord


def append_audit_event(
    run: RunRecord,
    actor: str,
    action: str,
    detail: str,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one consistently-shaped event to the run's durable audit log."""
    event = AuditEvent(
        id=f"AUD-{uuid4().hex[:10].upper()}",
        actor=actor,
        action=action,
        detail=detail,
        metadata=metadata or {},
    )
    run.audit.append(event)
    return event
