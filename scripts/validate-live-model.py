from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation import EvaluationService  # noqa: E402
from app.model_adapter import create_model_adapter  # noqa: E402
from app.retrieval import create_retriever  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run sealed cases through the real local Qwen adapter."
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8091/v1")
    parser.add_argument("--model", default="OpenVINO/Qwen3-VL-8B-Instruct-int4-ov")
    parser.add_argument("--api-key", default="harbor-local-model-token")
    parser.add_argument("--case-limit", type=int, default=1)
    parser.add_argument("--case-offset", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    retriever = create_retriever(
        PROJECT_ROOT / "data",
        "memory",
        "sqlite:///unused.db",
        "lexical-feature-baseline",
        "",
        "",
    )
    adapter = create_model_adapter(
        enabled=True,
        endpoint=args.endpoint,
        model=args.model,
        provider="local-openvino",
        timeout_seconds=args.timeout,
        api_key=args.api_key,
    )
    evaluator = EvaluationService(
        PROJECT_ROOT / "data" / "evaluation" / "manifest.json",
        retriever,
        "harbor-live-validation-capability-secret",
        adapter,
    )
    report = evaluator.run(
        tenant_id="live-model-validation",
        live_model=True,
        case_limit=args.case_limit,
        case_offset=args.case_offset,
    )
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
