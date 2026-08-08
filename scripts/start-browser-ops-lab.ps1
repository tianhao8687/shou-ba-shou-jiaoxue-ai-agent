param(
    [string]$Python = "python",
    [int]$Port = 8092
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot ".runtime"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$env:OPS_DATABASE_PATH = Join-Path $runtimeRoot "browser-fault-lab-v3.db"
$env:OPS_HEALTH_TOKEN = "harbor-e2e-health-token"
$env:OPS_ORACLE_TOKEN = "harbor-e2e-oracle-token"
$env:CAPABILITY_SIGNING_SECRET = "harbor-e2e-capability-secret-change-me"
$env:OPS_FAULT_INJECTION = "true"

Set-Location -LiteralPath $projectRoot
& $Python -m uvicorn ops_sandbox.app:app --host 127.0.0.1 --port $Port
