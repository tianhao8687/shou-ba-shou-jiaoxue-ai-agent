from __future__ import annotations

import hashlib
from typing import Any, Iterable

from ..schemas import ToolResult
from ..security import canonical_json


def tool_idempotency_key(
    run_id: str,
    plan_hash_value: str,
    step_id: str,
    payload: dict[str, Any],
) -> str:
    material = f"{run_id}:{plan_hash_value}:{step_id}:{canonical_json(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def prior_success(
    results: Iterable[ToolResult], idempotency_key: str
) -> ToolResult | None:
    for result in reversed(list(results)):
        if (
            result.idempotency_key == idempotency_key
            and result.status in {"succeeded", "skipped"}
        ):
            return result
    return None
