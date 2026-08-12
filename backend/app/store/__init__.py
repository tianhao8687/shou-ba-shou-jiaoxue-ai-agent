from __future__ import annotations

from .base import ConcurrencyError, LeaseLostError, Store
from .postgres import PostgresStore
from .sqlite import SQLiteStore


def create_store(
    database_url: str,
    *,
    postgres_pool_min_size: int = 1,
    postgres_pool_max_size: int = 10,
    postgres_pool_timeout_seconds: float = 10.0,
) -> Store:
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(
            database_url,
            pool_min_size=postgres_pool_min_size,
            pool_max_size=postgres_pool_max_size,
            pool_timeout_seconds=postgres_pool_timeout_seconds,
        )
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
