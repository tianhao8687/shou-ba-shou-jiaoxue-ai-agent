from __future__ import annotations

import logging
import signal
import socket
import threading

from prometheus_client import start_http_server

from .config import get_settings
from .runtime import build_agent_runtime


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("harbor.worker")


def resolve_worker_id(configured: str) -> str:
    """Resolve a stable, replica-unique identity inside a container or VM."""
    hostname = socket.gethostname()
    normalized = configured.strip()
    if not normalized or normalized.lower() == "auto":
        return f"worker-{hostname}"
    return normalized.replace("{hostname}", hostname)


def main() -> None:
    settings = get_settings()
    worker_id = resolve_worker_id(settings.worker_id)
    runtime = build_agent_runtime(settings, worker_id=worker_id)
    stopping = threading.Event()
    metrics_endpoint = start_http_server(settings.worker_metrics_port, addr="0.0.0.0")

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("received signal %s; stopping after the current checkpoint", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOGGER.info(
        "worker %s started (lease=%ss heartbeat=%ss metrics=:%s)",
        worker_id,
        settings.worker_lease_seconds,
        settings.worker_heartbeat_seconds,
        settings.worker_metrics_port,
    )
    runtime.worker.start_registry()
    try:
        while not stopping.is_set():
            processed = runtime.worker.run_once()
            if not processed:
                stopping.wait(settings.worker_poll_seconds)
    finally:
        runtime.worker.stop_registry()
        LOGGER.info("worker %s stopped", worker_id)
        del metrics_endpoint


if __name__ == "__main__":
    main()
