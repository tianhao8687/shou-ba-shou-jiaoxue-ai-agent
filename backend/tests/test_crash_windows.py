from __future__ import annotations

from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from app.schemas import (
    Check,
    Incident,
    PlanStep,
    RiskLevel,
    RollbackPlan,
    RunRecord,
    UserIdentity,
)
from app.security import CapabilityService
from app.store import LeaseLostError, SQLiteStore
from app.tools import InMemoryFaultLabClient, ToolExecutor


ACTOR = UserIdentity(
    username="crash-test@harbor.local",
    display_name="Crash Window Test",
    roles=["admin"],
)
PLAN_HASH = "b" * 64


def scale_step(
    experiment_id: str,
    *,
    step_id: str = "step-scale-workers",
    target_replicas: int = 5,
) -> PlanStep:
    return PlanStep(
        id=step_id,
        title="受控调整消费者副本",
        objective="验证副作用提交、响应丢失与补偿重试窗口中的幂等语义。",
        tool_name="scale_workers",
        tool_input={
            "experiment_id": experiment_id,
            "service": "invoice-worker",
            "target_replicas": target_replicas,
            "change_ticket": "CHG-8801",
        },
        evidence_ids=["OBS-QUEUE"],
        success_criteria=[
            Check(
                field="__result_status__",
                operator="eq",
                value="succeeded",
                description="工具必须给出明确结果",
            )
        ],
        rollback=RollbackPlan(
            mode="manual",
            rationale="本用例单独控制补偿调用，不允许递归补偿。",
        ),
        risk=RiskLevel.MEDIUM,
        rationale="所有参数固定绑定到同一实验、计划哈希与变更单。",
    )


def executor(client) -> ToolExecutor:
    return ToolExecutor(
        client,
        CapabilityService("crash-window-capability-secret-long-enough"),
    )


def execute(tool_executor: ToolExecutor, step: PlanStep, *, attempt: int = 1):
    return tool_executor.execute(
        step,
        run_id="RUN-CRASH-WINDOW",
        plan_hash_value=PLAN_HASH,
        actor=ACTOR,
        attempt=attempt,
        previous_results=[],
        job_id="JOB-CRASH-WINDOW",
        fencing_token=1,
        lease_guard=lambda: None,
    )


class CommitThenLoseAcknowledgement:
    """Tool boundary commits once, then drops the first response."""

    name = "commit-then-lose-ack"

    def __init__(self, delegate: InMemoryFaultLabClient) -> None:
        self.delegate = delegate
        self.drop_next_response = True

    def invoke(self, *args, **kwargs):
        response = self.delegate.invoke(*args, **kwargs)
        if self.drop_next_response:
            self.drop_next_response = False
            raise ConnectionResetError("response lost after durable tool commit")
        return response

    def health(self):
        return self.delegate.health()


def test_case_1_side_effect_committed_before_worker_checkpoint_is_not_repeated() -> None:
    lab = InMemoryFaultLabClient()
    experiment_id, _ = lab.create_experiment("queue_backlog")
    tool_executor = executor(lab)
    step = scale_step(experiment_id)

    committed_but_uncheckpointed = execute(tool_executor, step)
    assert committed_but_uncheckpointed.status == "succeeded"

    # Simulate worker death: its returned ToolResult never reaches RunRecord.
    recovered = execute(tool_executor, step, attempt=2)

    assert recovered.status == "skipped"
    assert recovered.idempotency_key == committed_but_uncheckpointed.idempotency_key
    assert lab.get_state(experiment_id)["effects"] == 1


def test_case_2_commit_ack_loss_returns_first_result_on_same_key_retry() -> None:
    lab = InMemoryFaultLabClient()
    experiment_id, _ = lab.create_experiment("queue_backlog")
    tool_executor = executor(CommitThenLoseAcknowledgement(lab))
    step = scale_step(experiment_id)

    unknown = execute(tool_executor, step)
    recovered = execute(tool_executor, step, attempt=2)

    assert unknown.status == "unknown"
    assert unknown.output["retryable_with_same_key"] is True
    assert recovered.status == "skipped"
    assert recovered.idempotency_key == unknown.idempotency_key
    assert recovered.output["replicas"] == 5
    assert lab.get_state(experiment_id)["effects"] == 1


def _incident() -> Incident:
    return Incident(
        title="持久任务崩溃窗口测试",
        summary="验证任务领取后的运行记录、租约和隔离令牌不会因连接重建而丢失。",
        severity="P2",
        service="crash-window-api",
        environment="lab",
        symptoms=["模拟数据库连接中断"],
    )


def test_case_3_stale_worker_commit_is_rejected_after_lease_recovery(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'case-3.db'}")
    store.initialize()
    run = RunRecord(id="RUN-CASE-3", incident=_incident())
    store.save_run_and_enqueue(run, "start")
    old = store.claim_job("worker-a", 0)
    new = store.claim_job("worker-b", 30)
    assert old is not None and new is not None

    stale = store.get_run(run.id)
    assert stale is not None
    stale.diagnosis = "stale writer"
    with pytest.raises(LeaseLostError):
        store.save_run_with_lease(stale, old.id, "worker-a", old.fencing_token)

    assert new.fencing_token == old.fencing_token + 1
    assert store.get_run(run.id).diagnosis != "stale writer"


def test_case_4_concurrent_workers_have_exactly_one_claim_winner(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'case-4.db'}")
    store.initialize()
    run = RunRecord(id="RUN-CASE-4", incident=_incident())
    _, job = store.save_run_and_enqueue(run, "start")
    workers = 24
    barrier = Barrier(workers)
    lock = Lock()
    claims = []

    def claim(index: int) -> None:
        barrier.wait()
        result = store.claim_job(f"worker-{index}", 30)
        if result is not None:
            with lock:
                claims.append(result)

    threads = [Thread(target=claim, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert [claim.id for claim in claims] == [job.id]


def test_case_5_claimed_run_survives_store_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "case-5.db"
    first_store = SQLiteStore(f"sqlite:///{database}")
    first_store.initialize()
    run = RunRecord(id="RUN-CASE-5", incident=_incident())
    _, job = first_store.save_run_and_enqueue(run, "start")
    first_claim = first_store.claim_job("worker-before-restart", 0)
    assert first_claim is not None

    # A new Store instance represents a fresh process and connection epoch.
    recovered_store = SQLiteStore(f"sqlite:///{database}")
    recovered_store.initialize()
    persisted = recovered_store.get_run(run.id)
    recovered_claim = recovered_store.claim_job("worker-after-restart", 30)

    assert persisted is not None
    assert persisted.id == run.id
    assert recovered_claim is not None
    assert recovered_claim.id == job.id
    assert recovered_claim.fencing_token == first_claim.fencing_token + 1


def test_case_7_compensation_commit_ack_loss_does_not_repeat_side_effect() -> None:
    lab = InMemoryFaultLabClient()
    experiment_id, _ = lab.create_experiment("queue_backlog")
    execute(executor(lab), scale_step(experiment_id, target_replicas=5))
    assert lab.get_state(experiment_id)["effects"] == 1

    compensation = scale_step(
        experiment_id,
        step_id="step-rollback-scale-workers",
        target_replicas=2,
    )
    compensation_executor = executor(CommitThenLoseAcknowledgement(lab))
    unknown = execute(compensation_executor, compensation)
    recovered = execute(compensation_executor, compensation, attempt=2)

    assert unknown.status == "unknown"
    assert recovered.status == "skipped"
    assert recovered.idempotency_key == unknown.idempotency_key
    assert lab.get_state(experiment_id)["effects"] == 2
    assert lab.get_state(experiment_id)["state"]["replicas"] == 2
