from app import worker_main


def test_worker_id_template_expands_to_replica_hostname(monkeypatch) -> None:
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "replica-a1b2")

    assert worker_main.resolve_worker_id("compose-{hostname}") == "compose-replica-a1b2"
    assert worker_main.resolve_worker_id("auto") == "worker-replica-a1b2"
    assert worker_main.resolve_worker_id("") == "worker-replica-a1b2"
    assert worker_main.resolve_worker_id("explicit-worker") == "explicit-worker"
