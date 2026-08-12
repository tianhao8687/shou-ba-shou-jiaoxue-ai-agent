from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import ValidationError

from .schemas import PlanStep, PolicyDecision, PolicyIssue, RiskLevel
from .registry import SERVICE_REGISTRY
from .security import canonical_json
from .tools import RISK_WEIGHT, TOOL_REGISTRY, effective_risk


class PolicyCompiler:
    """Deterministically compiles an untrusted model plan into an executable plan."""

    def __init__(self, max_steps: int = 12) -> None:
        self.max_steps = max_steps

    def compile(
        self,
        plan: list[PlanStep],
        *,
        phase: Literal["investigation", "remediation"],
        allowed_evidence_ids: set[str],
        observation_ids: set[str],
        experiment_id: str | None,
        service_id: str | None = None,
        observation_context: dict[str, object] | None = None,
    ) -> tuple[list[PlanStep], PolicyDecision]:
        issues: list[PolicyIssue] = []
        if not plan:
            issues.append(PolicyIssue(code="EMPTY_PLAN", message="模型没有生成可执行步骤。"))
        if len(plan) > self.max_steps:
            issues.append(
                PolicyIssue(code="PLAN_TOO_LONG", message=f"计划超过 {self.max_steps} 步上限。")
            )

        step_ids = [step.id for step in plan]
        if len(step_ids) != len(set(step_ids)):
            issues.append(PolicyIssue(code="DUPLICATE_STEP_ID", message="计划包含重复步骤 ID。"))
        id_set = set(step_ids)
        compiled: list[PlanStep] = []
        required_roles: set[str] = set()
        maximum_risk = RiskLevel.LOW
        observed = observation_context or {}
        service = SERVICE_REGISTRY.get(service_id) if service_id else None
        for step in plan:
            spec = TOOL_REGISTRY.get(step.tool_name)
            if spec is None:
                issues.append(
                    PolicyIssue(
                        code="UNKNOWN_TOOL", step_id=step.id, message=f"工具 {step.tool_name} 未注册。"
                    )
                )
                continue
            if service is not None and step.tool_name not in service.allowed_tools:
                issues.append(
                    PolicyIssue(
                        code="TOOL_NOT_ALLOWED_FOR_SERVICE",
                        step_id=step.id,
                        message=(
                            f"工具 {step.tool_name} 不在服务 {service.service_id} 的注册合同中。"
                        ),
                    )
                )
            try:
                validated_input = spec.input_model.model_validate(step.tool_input).model_dump()
            except ValidationError as exc:
                issues.append(
                    PolicyIssue(
                        code="INVALID_TOOL_INPUT",
                        step_id=step.id,
                        message=f"工具参数不符合严格 Schema：{exc.errors()[0]['msg']}",
                    )
                )
                continue
            if experiment_id and validated_input.get("experiment_id") != experiment_id:
                issues.append(
                    PolicyIssue(
                        code="EXPERIMENT_BINDING_MISMATCH",
                        step_id=step.id,
                        message="工具参数中的 experiment_id 与当前事件不一致。",
                    )
                )
            if phase == "investigation" and not spec.read_only:
                issues.append(
                    PolicyIssue(
                        code="WRITE_DURING_INVESTIGATION",
                        step_id=step.id,
                        message="调查阶段只允许 read_only 工具。",
                    )
                )
            unknown_evidence = set(step.evidence_ids) - allowed_evidence_ids
            if unknown_evidence:
                issues.append(
                    PolicyIssue(
                        code="UNKNOWN_EVIDENCE",
                        step_id=step.id,
                        message=f"步骤引用了不存在的证据：{sorted(unknown_evidence)}",
                    )
                )
            if not spec.read_only and not observation_ids.intersection(step.evidence_ids):
                issues.append(
                    PolicyIssue(
                        code="WRITE_WITHOUT_OBSERVATION",
                        step_id=step.id,
                        message="写步骤至少需要一条实际工具观测证据。",
                    )
                )
            required_fields = spec.required_observation_fields
            if required_fields and not required_fields.intersection(observed):
                issues.append(
                    PolicyIssue(
                        code="TOOL_NOT_SUPPORTED_BY_OBSERVATION",
                        step_id=step.id,
                        message=(
                            f"工具 {step.tool_name} 缺少适用观测；至少需要 "
                            f"{sorted(required_fields)} 之一。"
                        ),
                    )
                )
            unknown_dependencies = set(step.depends_on) - id_set
            if unknown_dependencies:
                issues.append(
                    PolicyIssue(
                        code="UNKNOWN_DEPENDENCY",
                        step_id=step.id,
                        message=f"步骤依赖不存在的 ID：{sorted(unknown_dependencies)}",
                    )
                )
            if not step.success_criteria:
                issues.append(
                    PolicyIssue(
                        code="MISSING_SUCCESS_CRITERIA",
                        step_id=step.id,
                        message="每个步骤都必须声明机器可检查的成功条件。",
                    )
                )
            risk = effective_risk(step)
            if RISK_WEIGHT[RiskLevel(step.risk)] < RISK_WEIGHT[spec.risk]:
                issues.append(
                    PolicyIssue(
                        code="RISK_UPGRADED",
                        step_id=step.id,
                        message=f"模型声明风险低于注册表；已从 {step.risk} 提升到 {spec.risk}。",
                        blocking=False,
                    )
                )
            if RISK_WEIGHT[risk] > RISK_WEIGHT[maximum_risk]:
                maximum_risk = risk
            required_roles.add(spec.required_role)
            validated_rollback = step.rollback
            if step.rollback.mode == "tool":
                rollback_name = step.rollback.tool_name or ""
                rollback_spec = TOOL_REGISTRY.get(rollback_name)
                if rollback_name != step.tool_name:
                    issues.append(
                        PolicyIssue(
                            code="ROLLBACK_TOOL_MISMATCH",
                            step_id=step.id,
                            message="自动回滚只能使用与主动作相同的受控工具，防止借回滚夹带新副作用。",
                        )
                    )
                if (
                    spec.rollback_contract != "same-tool"
                    or rollback_name != step.tool_name
                    or (
                        rollback_spec is not None
                        and rollback_spec.rollback_contract != "same-tool"
                    )
                ):
                    issues.append(
                        PolicyIssue(
                            code="AUTOMATIC_ROLLBACK_NOT_ALLOWED",
                            step_id=step.id,
                            message=(
                                f"工具 {step.tool_name} 的注册合同不允许自动补偿；"
                                "仅 same-tool 回滚合同可以进入自动回滚。"
                            ),
                        )
                    )
                if rollback_spec is None:
                    issues.append(
                        PolicyIssue(
                            code="UNKNOWN_ROLLBACK_TOOL",
                            step_id=step.id,
                            message=f"回滚工具 {rollback_name or '(missing)'} 未注册。",
                        )
                    )
                else:
                    try:
                        rollback_input = rollback_spec.input_model.model_validate(
                            step.rollback.tool_input
                        ).model_dump()
                    except ValidationError as exc:
                        issues.append(
                            PolicyIssue(
                                code="INVALID_ROLLBACK_INPUT",
                                step_id=step.id,
                                message=f"回滚参数不符合严格 Schema：{exc.errors()[0]['msg']}",
                            )
                        )
                    else:
                        if experiment_id and rollback_input.get("experiment_id") != experiment_id:
                            issues.append(
                                PolicyIssue(
                                    code="ROLLBACK_EXPERIMENT_BINDING_MISMATCH",
                                    step_id=step.id,
                                    message="回滚参数中的 experiment_id 与当前事件不一致。",
                                )
                            )
                        validated_rollback = step.rollback.model_copy(
                            update={"tool_input": rollback_input}
                        )
                        required_roles.add(rollback_spec.required_role)
                        if RISK_WEIGHT[rollback_spec.risk] > RISK_WEIGHT[maximum_risk]:
                            maximum_risk = rollback_spec.risk
            compiled.append(
                step.model_copy(
                    update={
                        "risk": risk,
                        "tool_input": validated_input,
                        "rollback": validated_rollback,
                    }
                )
            )

        if self._has_cycle(compiled):
            issues.append(PolicyIssue(code="CYCLIC_PLAN", message="计划依赖图存在环，拒绝执行。"))

        blocking = [issue for issue in issues if issue.blocking]
        if blocking:
            return compiled, PolicyDecision(
                accepted=False,
                effective_risk=maximum_risk,
                required_roles=sorted(required_roles),
                issues=issues,
            )
        serialized = [step.model_dump(mode="json") for step in compiled]
        plan_hash_value = hashlib.sha256(canonical_json(serialized).encode("utf-8")).hexdigest()
        return compiled, PolicyDecision(
            accepted=True,
            plan_hash=plan_hash_value,
            effective_risk=maximum_risk,
            required_roles=sorted(required_roles),
            issues=issues,
        )

    @staticmethod
    def _has_cycle(plan: list[PlanStep]) -> bool:
        dependencies = {step.id: set(step.depends_on) for step in plan}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            for dependency in dependencies.get(step_id, set()):
                if dependency in dependencies and visit(dependency):
                    return True
            visiting.remove(step_id)
            visited.add(step_id)
            return False

        return any(visit(step_id) for step_id in dependencies)
