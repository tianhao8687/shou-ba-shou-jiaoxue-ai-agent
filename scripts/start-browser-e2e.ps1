param(
    [string]$Python = "python",
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$runtimeRoot = Join-Path $projectRoot ".runtime"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$env:APP_ENV = "e2e"
$env:DATABASE_URL = "sqlite:///$((Join-Path $runtimeRoot 'browser-harbor-v3.db').Replace('\', '/'))"
$env:AUTH_SIGNING_SECRET = "harbor-e2e-auth-secret-change-me"
$env:CAPABILITY_SIGNING_SECRET = "harbor-e2e-capability-secret-change-me"
$env:TOOL_MODE = "remote"
$env:TOOL_SANDBOX_URL = "http://127.0.0.1:8092"
$env:TOOL_SANDBOX_TOKEN = "harbor-e2e-health-token"
$env:LAB_ORACLE_TOKEN = "harbor-e2e-oracle-token"
$env:EMBEDDED_WORKER = "true"
$env:MODEL_ENABLED = "false"
$env:MODEL_FIXTURE_MODE = "true"
$env:WORKER_POLL_SECONDS = "0.1"
$env:WORKER_HEARTBEAT_SECONDS = "1"
$env:WORKER_LEASE_SECONDS = "5"

Set-Location -LiteralPath $backendRoot
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
