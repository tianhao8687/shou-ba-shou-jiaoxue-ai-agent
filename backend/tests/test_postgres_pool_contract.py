from __future__ import annotations

from contextlib import contextmanager

from app.store import PostgresStore


class _Cursor:
    def __init__(self, connection, events) -> None:
        self.connection = connection
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.events.append((self.connection.identity, statement))


class _Connection:
    def __init__(self, identity: object, events: list[tuple[object, str]]) -> None:
        self.identity = identity
        self.events = events

    def cursor(self) -> _Cursor:
        return _Cursor(self, self.events)


class _RecordingPool:
    def __init__(self, **configuration) -> None:
        self.configuration = configuration
        self.events: list[tuple[object, str]] = []
        self.checkouts = 0
        self.opens = 0
        self.closed = False
        self.connection_instance = _Connection(object(), self.events)

    def open(self, *, wait: bool, timeout: float) -> None:
        del wait, timeout
        self.opens += 1

    def wait(self, *, timeout: float) -> None:
        del timeout

    @contextmanager
    def connection(self, *, timeout: float):
        del timeout
        self.checkouts += 1
        yield self.connection_instance

    def close(self, *, timeout: float) -> None:
        del timeout
        self.closed = True


def test_advisory_lock_and_unlock_use_one_pooled_connection() -> None:
    pools: list[_RecordingPool] = []

    def factory(**configuration):
        pool = _RecordingPool(**configuration)
        pools.append(pool)
        return pool

    store = PostgresStore(
        "postgresql://database.invalid/harbor",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout_seconds=3,
        pool_factory=factory,
    )
    try:
        with store.model_inference_slot():
            assert pools[0].checkouts == 1

        pool = pools[0]
        assert pool.configuration["min_size"] == 1
        assert pool.configuration["max_size"] == 4
        assert pool.opens == 1
        assert pool.checkouts == 1
        assert len(pool.events) == 2
        assert pool.events[0][0] is pool.events[1][0]
        assert "pg_advisory_lock" in pool.events[0][1]
        assert "pg_advisory_unlock" in pool.events[1][1]
    finally:
        store.close()

    assert pools[0].closed is True
