from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CLUSTER_NAME = "harbor-agentops"
NAMESPACE = "harbor-sandbox"
SERVICE_ACCOUNT = "harbor-agentops-connector"
KIND_VERSION = "v0.32.0"
KIND_ASSETS = {
    ("Windows", "AMD64"): (
        "kind-windows-amd64",
        "0bcb2d1cfedc1912d664014db716937e8a0e843e91c6807b4db2025dbc8989fa",
    ),
    ("Linux", "AMD64"): (
        "kind-linux-amd64",
        "50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54",
    ),
}
DEFAULT_HEALTH_TOKEN = "harbor-local-kubernetes-health-token-change-me"
DEFAULT_CAPABILITY_SECRET = "harbor-local-capability-secret-change-me"


class KindLabError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    capture: bool = False,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise KindLabError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{detail[-1500:]}"
        )
    return completed


def require(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise KindLabError(f"{name} was not found on PATH")
    return executable


def kind_executable() -> str:
    system = platform.system()
    machine = platform.machine().upper()
    if machine in {"X86_64", "AMD64"}:
        machine = "AMD64"
    asset = KIND_ASSETS.get((system, machine))
    if asset is None:
        raise KindLabError(f"unsupported kind host: {system}/{machine}")
    filename, expected_sha256 = asset
    suffix = ".exe" if system == "Windows" else ""
    destination = ROOT / ".tools" / "kind" / KIND_VERSION / f"kind{suffix}"
    if destination.is_file():
        observed = hashlib.sha256(destination.read_bytes()).hexdigest()
        if observed == expected_sha256:
            return str(destination)
        raise KindLabError(
            f"existing kind binary checksum mismatch: expected {expected_sha256}, got {observed}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://kind.sigs.k8s.io/dl/{KIND_VERSION}/{filename}"
    print(f"Downloading pinned kind {KIND_VERSION} from {url}", flush=True)
    try:
        with urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as temporary:
            while chunk := response.read(1024 * 1024):
                temporary.write(chunk)
            temporary_path = Path(temporary.name)
    except (OSError, URLError) as exc:
        raise KindLabError(f"could not download kind: {exc}") from exc
    observed = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
    if observed != expected_sha256:
        temporary_path.unlink(missing_ok=True)
        raise KindLabError(
            f"downloaded kind checksum mismatch: expected {expected_sha256}, got {observed}"
        )
    temporary_path.replace(destination)
    if system != "Windows":
        destination.chmod(0o755)
    return str(destination)


def cluster_exists(kind: str) -> bool:
    result = run([kind, "get", "clusters"], capture=True)
    return CLUSTER_NAME in result.stdout.splitlines()


def apply_secret(kubectl: str) -> None:
    health = os.getenv("KUBERNETES_CONNECTOR_HEALTH_TOKEN", DEFAULT_HEALTH_TOKEN)
    capability = os.getenv("CAPABILITY_SIGNING_SECRET", DEFAULT_CAPABILITY_SECRET)
    if len(health) < 24 or len(capability) < 24:
        raise KindLabError("connector secrets must contain at least 24 characters")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "harbor-kubernetes-connector-secrets",
            "namespace": NAMESPACE,
        },
        "type": "Opaque",
        "data": {
            "health-token": base64.b64encode(health.encode()).decode(),
            "capability-secret": base64.b64encode(capability.encode()).decode(),
        },
    }
    run(
        [kubectl, "apply", "-f", "-"],
        input_text=json.dumps(secret, separators=(",", ":")),
    )


def rbac_check(
    kubectl: str,
    verb: str,
    resource: str,
    expected: bool,
    *,
    resource_name: str | None = None,
    subresource: str | None = None,
) -> None:
    subject = f"system:serviceaccount:{NAMESPACE}:{SERVICE_ACCOUNT}"
    resource_spec = f"{resource}/{resource_name}" if resource_name else resource
    command = [
        kubectl,
        "auth",
        "can-i",
        "--as",
        subject,
        verb,
        resource_spec,
        "--namespace",
        NAMESPACE,
    ]
    if subresource:
        command.append(f"--subresource={subresource}")
    result = run(
        command,
        capture=True,
        check=False,
    )
    observed = result.stdout.strip().lower() == "yes"
    if observed != expected:
        raise KindLabError(
            "RBAC mismatch: "
            f"{verb} {resource}/{resource_name or '*'}"
            f" subresource={subresource or '-'} expected {expected}, "
            f"got {result.stdout.strip()} {result.stderr.strip()}"
        )


def wait_health(timeout: float = 120) -> dict[str, object]:
    token = os.getenv("KUBERNETES_CONNECTOR_HEALTH_TOKEN", DEFAULT_HEALTH_TOKEN)
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            request = Request(
                "http://127.0.0.1:18094/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=3) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise KindLabError(f"connector did not become ready: {last_error}")


def verify(kubectl: str) -> None:
    checks = [
        ("get", "deployments.apps", "demo-api", None, True),
        ("get", "deployments.apps", "demo-api", "scale", True),
        ("update", "deployments.apps", "demo-api", "scale", True),
        ("list", "pods", None, None, True),
        ("get", "secrets", "harbor-kubernetes-connector-secrets", None, False),
        ("create", "pods", None, None, False),
        ("update", "deployments.apps", "demo-api", None, False),
        ("create", "rolebindings.rbac.authorization.k8s.io", None, None, False),
    ]
    for verb, resource, resource_name, subresource, expected in checks:
        rbac_check(
            kubectl,
            verb,
            resource,
            expected,
            resource_name=resource_name,
            subresource=subresource,
        )
    health = wait_health()
    if health.get("write_scope") != ["deployments/scale"]:
        raise KindLabError(f"unexpected connector write scope: {health}")
    print(json.dumps({"rbac": "passed", "health": health}, ensure_ascii=False, indent=2))


def up(_: argparse.Namespace) -> None:
    docker = require("docker")
    kubectl = require("kubectl")
    kind = kind_executable()
    run([docker, "info"], capture=True)
    if not cluster_exists(kind):
        run(
            [
                kind,
                "create",
                "cluster",
                "--name",
                CLUSTER_NAME,
                "--config",
                str(ROOT / "kubernetes" / "kind" / "cluster.yaml"),
                "--wait",
                "180s",
            ]
        )
    run(
        [
            docker,
            "build",
            "-t",
            "harbor-kubernetes-connector:dev",
            "-f",
            "kubernetes_connector/Dockerfile",
            ".",
        ]
    )
    run(
        [
            kind,
            "load",
            "docker-image",
            "--name",
            CLUSTER_NAME,
            "harbor-kubernetes-connector:dev",
        ]
    )
    run([kubectl, "apply", "-f", str(ROOT / "kubernetes" / "kind" / "sandbox.yaml")])
    apply_secret(kubectl)
    run(
        [
            kubectl,
            "rollout",
            "restart",
            "deployment/harbor-kubernetes-connector",
            "--namespace",
            NAMESPACE,
        ]
    )
    for deployment in ("demo-api", "harbor-kubernetes-connector"):
        run(
            [
                kubectl,
                "rollout",
                "status",
                f"deployment/{deployment}",
                "--namespace",
                NAMESPACE,
                "--timeout=180s",
            ]
        )
    verify(kubectl)
    print("kind staging lab is ready at http://127.0.0.1:18094")


def verify_command(_: argparse.Namespace) -> None:
    verify(require("kubectl"))


def down(_: argparse.Namespace) -> None:
    kind = kind_executable()
    if cluster_exists(kind):
        run([kind, "delete", "cluster", "--name", CLUSTER_NAME])
    print(f"kind cluster {CLUSTER_NAME} is absent")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Create and verify the namespace-scoped Harbor kind staging lab."
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("up").set_defaults(handler=up)
    commands.add_parser("verify").set_defaults(handler=verify_command)
    commands.add_parser("down").set_defaults(handler=down)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
        return 0
    except KindLabError as exc:
        print(f"kind lab failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
