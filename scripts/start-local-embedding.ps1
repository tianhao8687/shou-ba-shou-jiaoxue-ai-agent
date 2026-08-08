param(
    [string]$Python = "D:\product-evidence-guard\.venv\Scripts\python.exe",
    [string]$ModelDir = "",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8093,
    [string]$Token = "harbor-local-embedding-token"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ModelDir) {
    $workspaceRoot = Split-Path -Parent (Split-Path -Parent $projectRoot)
    $ModelDir = Join-Path $workspaceRoot "work\models\Qwen3-Embedding-0.6B-int4-cw-ov"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "openvino_model.xml"))) {
    throw "Embedding model not found: $ModelDir"
}

& $Python (Join-Path $projectRoot "local_embedding_service\server.py") --model-dir $ModelDir --host $HostAddress --port $Port --token $Token
