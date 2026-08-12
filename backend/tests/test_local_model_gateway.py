from __future__ import annotations

import threading
import time

import pytest

from local_model_service.server import (
    ModelQueueFullError,
    ModelQueueTimeoutError,
    Runtime,
)


class BlockingPipeline:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def generate(self, prompt: str, *, max_new_tokens: int) -> str:
        del prompt, max_new_tokens
        self.started.set()
        assert self.release.wait(timeout=2)
        return '{"status":"ok"}'


def _start_blocking_generation(runtime: Runtime, release: threading.Event) -> threading.Thread:
    def generate() -> None:
        runtime.generate("first", 16)

    thread = threading.Thread(target=generate, daemon=True)
    thread.start()
    return thread


def test_local_model_gateway_rejects_when_bounded_queue_is_full(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()
    runtime = Runtime(
        tmp_path,
        "CPU",
        max_pending_requests=0,
        queue_timeout_seconds=0.2,
    )
    runtime.pipeline = BlockingPipeline(started, release)
    thread = _start_blocking_generation(runtime, release)
    assert started.wait(timeout=1)

    try:
        with pytest.raises(ModelQueueFullError):
            runtime.generate("second", 16)
    finally:
        release.set()
        thread.join(timeout=2)

    metrics = runtime.metrics_snapshot()
    assert metrics["request_count"] == 1
    assert metrics["rejected_count"] == 1
    assert metrics["active_requests"] == 0
    assert metrics["queued_requests"] == 0


def test_local_model_gateway_times_out_an_admitted_waiter(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()
    runtime = Runtime(
        tmp_path,
        "CPU",
        max_pending_requests=1,
        queue_timeout_seconds=0.03,
    )
    runtime.pipeline = BlockingPipeline(started, release)
    thread = _start_blocking_generation(runtime, release)
    assert started.wait(timeout=1)

    try:
        with pytest.raises(ModelQueueTimeoutError):
            runtime.generate("queued", 16)
    finally:
        release.set()
        thread.join(timeout=2)

    metrics = runtime.metrics_snapshot()
    assert metrics["request_count"] == 1
    assert metrics["queue_timeout_count"] == 1
    assert metrics["active_requests"] == 0
    assert metrics["queued_requests"] == 0


def test_timed_out_waiter_does_not_remove_another_waiter_from_metrics(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()
    first_waiter_finished = threading.Event()
    runtime = Runtime(
        tmp_path,
        "CPU",
        max_pending_requests=2,
        queue_timeout_seconds=0.5,
    )
    runtime.pipeline = BlockingPipeline(started, release)
    active = _start_blocking_generation(runtime, release)
    assert started.wait(timeout=1)

    first_error: list[Exception] = []
    second_result: list[str] = []

    def first_waiter() -> None:
        try:
            runtime.generate("first-waiter", 16)
        except Exception as exc:  # captured for an assertion in the parent thread
            first_error.append(exc)
        finally:
            first_waiter_finished.set()

    def second_waiter() -> None:
        result, _, _ = runtime.generate("second-waiter", 16)
        second_result.append(result)

    waiter_one = threading.Thread(target=first_waiter, daemon=True)
    waiter_one.start()
    deadline = time.monotonic() + 1
    while runtime.metrics_snapshot()["queued_requests"] != 1:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    # Stagger admission so waiter one times out while waiter two is still queued.
    time.sleep(0.2)
    waiter_two = threading.Thread(target=second_waiter, daemon=True)
    waiter_two.start()
    deadline = time.monotonic() + 1
    while runtime.metrics_snapshot()["queued_requests"] != 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    assert first_waiter_finished.wait(timeout=1)
    assert isinstance(first_error[0], ModelQueueTimeoutError)
    assert runtime.metrics_snapshot()["queued_requests"] == 1

    release.set()
    active.join(timeout=2)
    waiter_one.join(timeout=2)
    waiter_two.join(timeout=2)
    assert second_result == ['{"status":"ok"}']
    assert runtime.metrics_snapshot()["queued_requests"] == 0
