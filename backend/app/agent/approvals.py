from __future__ import annotations

from typing import Any

from ..schemas import Approval, ApprovalVote, RunStatus, UserIdentity, utc_now
from ..security import AuthorizationError, require_all_roles
from ..store import ConcurrencyError
from .transitions import apply_transition


class ApprovalMixin:
    store: Any

    def decide(
        self,
        run_id: str,
        decision: str,
        user: UserIdentity,
        note: str,
        expected_version: int,
    ):
        run = self.require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError("当前运行不在等待审批状态")
        if run.current_node != "approval":
            raise ValueError("审批状态与显式工作流节点不一致")
        if not run.policy.accepted or not run.policy.plan_hash:
            raise ValueError("计划没有通过策略编译，不能审批")
        if run.approval.plan_hash != run.policy.plan_hash:
            raise ValueError("审批绑定的计划哈希已失效")
        require_all_roles(user, run.approval.required_roles)
        if (
            run.approval.separation_of_duties
            and run.approval.requester
            and user.username.lower() == run.approval.requester.lower()
        ):
            raise AuthorizationError("requester cannot approve or deny the same change")
        if any(
            vote.approver.lower() == user.username.lower()
            for vote in run.approval.votes
        ):
            raise ValueError("the same subject cannot vote twice")
        vote = ApprovalVote(
            approver=user.username,
            display_name=user.display_name,
            roles=user.roles,
            tenant_id=user.tenant_id,
            plan_hash=run.policy.plan_hash,
            decision=decision,
            note=note,
        )
        votes = [*run.approval.votes, vote]
        if decision == "approve":
            if len(votes) < run.approval.required_approvals:
                run.approval = run.approval.model_copy(
                    update={"decision": "pending", "votes": votes}
                )
                self.audit(
                    run,
                    user.username,
                    "approval.vote_recorded",
                    (
                        f"已记录第 {len(votes)} 票；还需要 "
                        f"{run.approval.required_approvals - len(votes)} 个不同主体。"
                    ),
                    {
                        "plan_hash": run.policy.plan_hash,
                        "votes": len(votes),
                        "required_approvals": run.approval.required_approvals,
                    },
                )
                return self.store.save_run(run)
            run.approval = Approval(
                required=True,
                decision="approved",
                plan_hash=run.policy.plan_hash,
                required_roles=run.approval.required_roles,
                required_approvals=run.approval.required_approvals,
                separation_of_duties=run.approval.separation_of_duties,
                requester=run.approval.requester,
                votes=votes,
                decided_by=user.username,
                decided_roles=user.roles,
                decided_at=utc_now(),
                note=note,
            )
            apply_transition(
                run,
                "execute",
                actor=user.username,
                reason="独立审批人数达到 quorum；不可变计划重新入队。",
                status=RunStatus.QUEUED,
            )
            self.audit(
                run,
                user.username,
                "approval.approved",
                "独立审批人数达到 quorum；不可变计划已重新入队。",
                {
                    "plan_hash": run.policy.plan_hash,
                    "roles": user.roles,
                    "approvers": [item.approver for item in votes],
                    "required_approvals": run.approval.required_approvals,
                },
            )
            self.store.save_run_and_enqueue(run, "resume")
            return run
        if decision != "deny":
            raise ValueError("decision must be approve or deny")
        run.approval = Approval(
            required=True,
            decision="denied",
            plan_hash=run.policy.plan_hash,
            required_roles=run.approval.required_roles,
            required_approvals=run.approval.required_approvals,
            separation_of_duties=run.approval.separation_of_duties,
            requester=run.approval.requester,
            votes=votes,
            decided_by=user.username,
            decided_roles=user.roles,
            decided_at=utc_now(),
            note=note,
        )
        run.resolution = "审批拒绝；没有执行任何待批准写动作，已转人工。"
        apply_transition(
            run,
            "handed_off",
            actor=user.username,
            reason=run.resolution,
            status=RunStatus.HANDED_OFF,
        )
        self.audit(run, user.username, "approval.denied", run.resolution)
        return self.store.save_run(run)
