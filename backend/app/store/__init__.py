from __future__ import annotations

from .base import ConcurrencyError, LeaseLostError, Store
from .postgres import PostgresStore
from .sqlite import SQLiteStore


def create_store(database_url: str) -> Store:
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(database_url)
    if not database_url.startswith("sqlite:///"):
        raise ValueError("DATABASE_URL 仅支持 sqlite:/// 或 postgresql://")
    return SQLiteStore(database_url)


__all__ = [
    "ConcurrencyError",
    "LeaseLostError",
    "PostgresStore",
    "SQLiteStore",
    "Store",
    "create_store",
]
