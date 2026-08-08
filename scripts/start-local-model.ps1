param(
    [string]$Python = "D:\product-evidence-guard\.venv\Scripts\python.exe",
    [string]$ModelPath = "D:\product-evidence-guard\.models\Qwen3-VL-8B-Instruct-int4-ov",
    [int]$Port = 8091,
    [switch]$Eager
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$gateway = Join-Path $projectRoot "local_model_service\server.py"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "OpenVINO Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath $ModelPath -PathType Container)) {
    throw "Local Qwen model not found: $ModelPath"
}

$env:HARBOR_LOCAL_MODEL_PATH = $ModelPath
$arguments = @($gateway, "--port", "$Port")
if ($Eager) { $arguments += "--eager" }

& $Python @arguments
