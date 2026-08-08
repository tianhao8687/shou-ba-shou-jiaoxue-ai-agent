from __future__ import annotations

from prometheus_client import Counter, Histogram


RUNS_STARTED = Counter("harbor_agent_runs_started_total", "Agent runs started", ["scenario"])
NODE_TRANSITIONS = Counter("harbor_agent_node_transitions_total", "State graph node transitions", ["node", "status"])
NODE_LATENCY = Histogram(
    "harbor_agent_node_duration_seconds",
    "Measured state graph node duration",
    ["node"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 60, 180),
)
MODEL_CALLS = Counter("harbor_agent_model_calls_total", "Structured model calls", ["provider", "status"])
MODEL_LATENCY = Histogram(
    "harbor_agent_model_duration_seconds",
    "Structured model call duration",
    ["provider"],
    buckets=(0.01, 0.1, 0.5, 1, 5, 15, 30, 60, 180, 300, 600),
)
MODEL_SLOT_WAIT = Histogram(
    "harbor_agent_model_slot_wait_seconds",
    "Time spent waiting for the cross-worker local-model inference slot",
    buckets=(0.001, 0.01, 0.1, 1, 5, 15, 30, 60, 120, 180, 300, 600),
)
TOOL_CALLS = Counter("harbor_agent_tool_calls_total", "Tool calls", ["tool", "status", "client"])
TOOL_LATENCY = Histogram(
    "harbor_agent_tool_duration_seconds",
    "Measured tool duration",
    ["tool", "client"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 15),
)
APPROVALS = Counter("harbor_agent_approval_decisions_total", "Approval decisions", ["decision"])
CONCURRENCY_CONFLICTS = Counter("harbor_agent_concurrency_conflicts_total", "Optimistic concurrency conflicts")
