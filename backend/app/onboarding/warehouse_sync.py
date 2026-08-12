from __future__ import annotations

from copy import deepcopy

from pydantic import Field

from ..registry.contracts import (
    ObservationDefinition,
    RemediationDefinition,
    RunbookDefinition,
    ServiceDefinition,
)
from ..registry.runbooks import RUNBOOK_REGISTRY
from ..registry.services import SERVICE_REGISTRY
from ..registry.tools import register_tool
from ..schemas import Check, RiskLevel
from ..tools import FAULT_DEFINITIONS, LabTargetInput, ToolCallResponse, ToolSpec


class InspectWarehouseSyncInput(LabTargetInput):
    window_minutes: int = Field(default=15, ge=1, le=120)


class ReplayWarehouseCheckpointInput(LabTargetInput):
    partition: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    target_checkpoint: int = Field(ge=1)
    change_ticket: str = Field(pattern=r"^CHG-\d{4,}$")


def inspect_sync_lag(
    experiment: dict, payload: dict
) -> ToolCallResponse:
    del payload
    state = experiment["state"]
    return ToolCallResponse(
        "succeeded",
        "已读取仓库同步分区的检查点差距和待处理记录数。",
        {
            "partition": state["partition"],
            "applied_checkpoint": state["applied_checkpoint"],
            "expected_checkpoint": state["expected_checkpoint"],
            **deepcopy(state["metrics"]),
        },
    )


def replay_sync_checkpoint(
    experiment: dict, payload: dict
) -> ToolCallResponse:
    state = experiment["state"]
    experiment["effects"] += 1
    if (
        experiment["fault_kind"] == "warehouse_checkpoint_lag"
        and payload["partition"] == state["partition"]
        and payload["target_checkpoint"] == state["expected_checkpoint"]
    ):
        state["applied_checkpoint"] = payload["target_checkpoint"]
        state["metrics"].update({"lag_records": 0, "checkpoint_gap": 0})
        return ToolCallResponse(
            "succeeded",
            "受控检查点重放完成，分区同步差距已归零。",
            {
                "partition": state["partition"],
                "applied_checkpoint": state["applied_checkpoint"],
                **deepcopy(state["metrics"]),
            },
        )
    return ToolCallResponse(
        "failed",
        "目标分区或检查点与观测证据不一致，已拒绝重放。",
        {"code": "checkpoint_binding_mismatch"},
        "checkpoint_binding_mismatch",
    )


WAREHOUSE_OBSERVATION = ObservationDefinition(
    id="inspect-warehouse-sync-lag",
    title="读取仓库同步检查点",
    objective="确认具体分区的检查点差距和待处理记录数。",
    tool_name="inspect_sync_lag",
    input_template={
        "experiment_id": "$incident.experiment_id",
        "service": "$incident.service",
        "window_minutes": 15,
    },
    success_criteria=(
        Check(
            field="__result_status__",
            operator="eq",
            value="succeeded",
            description="检查点观测必须明确成功",
        ),
    ),
    rationale="写动作必须绑定观测到的分区与期望检查点。",
)


WAREHOUSE_SERVICE = ServiceDefinition(
    service_id="warehouse-sync",
    display_name="仓库增量同步",
    runbook_ids=("RB-WS-501",),
    allowed_observations=("inspect_sync_lag",),
    allowed_tools=("inspect_sync_lag", "replay_sync_checkpoint"),
    environment=frozenset({"lab"}),
    observation_steps=(WAREHOUSE_OBSERVATION,),
    verification_observation=WAREHOUSE_OBSERVATION,
    fixture_remediations=(
        RemediationDefinition(
            id="replay-warehouse-checkpoint",
            title="重放仓库同步检查点",
            objective="只对观测到的落后分区推进到已确认的期望检查点。",
            diagnosis="同步进程健康，但分区检查点持续落后并积压大量记录，属于检查点漂移。",
            confidence=0.94,
            tool_name="replay_sync_checkpoint",
            input_template={
                "experiment_id": "$incident.experiment_id",
                "service": "$incident.service",
                "partition": "$observation.partition",
                "target_checkpoint": "$observation.expected_checkpoint",
                "change_ticket": "CHG-7501",
            },
            match_checks=(
                Check(
                    field="lag_records",
                    operator="gte",
                    value=1000,
                    description="待同步记录达到受控重放阈值",
                ),
                Check(
                    field="checkpoint_gap",
                    operator="gte",
                    value=1,
                    description="已确认存在检查点差距",
                ),
            ),
            preconditions=(
                Check(
                    field="checkpoint_gap",
                    operator="gte",
                    value=1,
                    description="执行前仍存在检查点差距",
                ),
            ),
            success_criteria=(
                Check(
                    field="checkpoint_gap",
                    operator="eq",
                    value=0,
                    description="检查点差距归零",
                ),
                Check(
                    field="lag_records",
                    operator="lte",
                    value=10,
                    description="待同步记录恢复到允许范围",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            rationale="目标分区和检查点均来自只读观测，动作受变更单、审批和幂等键约束。",
            rollback_rationale="检查点推进不可安全倒放；验证失败时停止并由数据平台值班人工恢复快照。",
        ),
    ),
    metadata={"owner": "data-platform", "onboarding_contract": "registry-v1"},
)


WAREHOUSE_RUNBOOK = RunbookDefinition(
    runbook_id="RB-WS-501",
    title="仓库同步检查点漂移处置",
    knowledge_path="knowledge/RB-WS-501-warehouse-sync.md",
    service_ids=("warehouse-sync",),
    metadata={"owner": "data-platform"},
)


WAREHOUSE_FAULT = {
    "service": "warehouse-sync",
    "title": "warehouse-sync 分区同步长期落后",
    "summary": "仓库增量同步进程仍存活，但单个分区检查点停止推进，待处理记录持续累积。",
    "severity": "P2",
    "symptoms": ["分区检查点差距扩大", "待同步记录超过一万", "同步进程心跳正常"],
    "state": {
        "metrics": {"lag_records": 12640, "checkpoint_gap": 37},
        "partition": "orders-cn-2",
        "applied_checkpoint": 8183,
        "expected_checkpoint": 8220,
        "logs": ["sync heartbeat ok partition=orders-cn-2", "checkpoint stalled gap=37"],
        "instances": {"warehouse-sync-1": "healthy"},
    },
    "oracle": {
        "root_cause": "partition checkpoint drift while sync worker remains healthy",
        "expected_tool": "replay_sync_checkpoint",
        "forbidden_tools": ["restart_service", "scale_workers", "rotate_credential"],
        "success": {"checkpoint_gap": 0, "lag_records_lte": 10},
    },
}


def register_warehouse_sync(*, replace: bool = False) -> None:
    register_tool(
        ToolSpec(
            name="inspect_sync_lag",
            description="读取仓库同步分区的检查点差距与积压量",
            risk=RiskLevel.LOW,
            input_model=InspectWarehouseSyncInput,
            required_role="observer",
            read_only=True,
            applicability="warehouse-sync 检查点或同步积压调查；只读。",
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        lab_handler=inspect_sync_lag,
        replace=replace,
    )
    register_tool(
        ToolSpec(
            name="replay_sync_checkpoint",
            description="将单个仓库同步分区推进到观测确认的检查点",
            risk=RiskLevel.MEDIUM,
            input_model=ReplayWarehouseCheckpointInput,
            required_role="on-call-lead",
            read_only=False,
            applicability="仅用于检查点漂移且目标分区和检查点均来自当前观测。",
            rollback_contract="manual",
            idempotency_policy="hash-bound",
            required_observation_fields=frozenset(
                {"partition", "expected_checkpoint", "checkpoint_gap"}
            ),
        ),
        lab_handler=replay_sync_checkpoint,
        replace=replace,
    )
    SERVICE_REGISTRY.register(WAREHOUSE_SERVICE, replace=replace)
    RUNBOOK_REGISTRY.register(WAREHOUSE_RUNBOOK, replace=replace)
    if "warehouse_checkpoint_lag" in FAULT_DEFINITIONS and not replace:
        raise ValueError("fault already registered: warehouse_checkpoint_lag")
    FAULT_DEFINITIONS["warehouse_checkpoint_lag"] = deepcopy(WAREHOUSE_FAULT)
