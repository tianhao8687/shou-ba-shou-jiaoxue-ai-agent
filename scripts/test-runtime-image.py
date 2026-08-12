from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]

RUNTIME_ASSERTION = """
import json
from pathlib import Path

from app.retrieval_config import load_retrieval_config

path = Path('/app/config/retrieval.json')
config = load_retrieval_config(path)
assert path.is_file(), path
print(json.dumps({
    'contract': 'harbor-agentops-runtime-config/v1',
    'config_path': str(path),
    'config_loaded': True,
    'schema': config.schema_name,
    'config_hash': config.config_hash,
}, sort_keys=True))
""".strip()


def run(command: list[str]) -> None:
    child_environment = os.environ.copy()
    if os.name == "nt" and not str(ROOT).isascii():
        child_environment.setdefault("DOCKER_BUILDKIT", "0")
    subprocess.run(command, cwd=ROOT, env=child_environment, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the real runtime stage and prove retrieval config is loadable."
    )
    parser.add_argument(
        "--tag", default="harbor-agentops-runtime-contract:local"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="disable Docker build cache"
    )
    args = parser.parse_args()

    build = [
        "docker",
        "build",
        "--target",
        "runtime",
        "--file",
        "backend/Dockerfile",
        "--tag",
        args.tag,
    ]
    if args.no_cache:
        build.append("--no-cache")
    build.append(".")
    run(build)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            args.tag,
            "-c",
            RUNTIME_ASSERTION,
        ]
    )


if __name__ == "__main__":
    main()
