from __future__ import annotations

from datetime import datetime, timezone

from ..schemas import RunRecord

def _with_version(payload: str | dict, version: int) -> RunRecord:
    run = (
        RunRecord.model_validate_json(payload)
        if isinstance(payload, str)
        else RunRecord.model_validate(payload)
    )
    run.version = version
    return run


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
