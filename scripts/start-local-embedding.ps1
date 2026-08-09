param(
    [string]$Python = $env:HARBOR_PYTHON,
    [string]$ModelDir = $env:HARBOR_LOCAL_EMBEDDING_PATH,
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8093,
    [string]$Device = $env:HARBOR_LOCAL_EMBEDDING_DEVICE,
    [string]$Token = $env:HARBOR_EMBEDDING_GATEWAY_TOKEN,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = "python"
}
if ([string]::IsNullOrWhiteSpace($Device)) {
    $Device = "CPU"
}
$pythonCandidate = $Python
if (-not [System.IO.Path]::IsPathRooted($pythonCandidate) -and $pythonCandidate -match '[\\/]') {
    $pythonCandidate = Join-Path $projectRoot $pythonCandidate
}
if (Test-Path -LiteralPath $pythonCandidate -PathType Leaf) {
    $pythonExecutable = (Resolve-Path -LiteralPath $pythonCandidate).Path
} else {
    $pythonCommand = Get-Command -Name $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) {
        throw "Python runtime not found: '$Python'. Pass -Python or set HARBOR_PYTHON."
    }
    $pythonExecutable = $pythonCommand.Source
}

if ([string]::IsNullOrWhiteSpace($ModelDir)) {
    $ModelDir = Join-Path $projectRoot ".models\Qwen3-Embedding-0.6B-int4-cw-ov"
} elseif (-not [System.IO.Path]::IsPathRooted($ModelDir)) {
    $ModelDir = Join-Path $projectRoot $ModelDir
}
if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "openvino_model.xml"))) {
    throw "Embedding model not found: '$ModelDir'. Pass -ModelDir, set HARBOR_LOCAL_EMBEDDING_PATH, or place it under '.models\Qwen3-Embedding-0.6B-int4-cw-ov'."
}
$resolvedModelDir = (Resolve-Path -LiteralPath $ModelDir).Path

if ([string]::IsNullOrWhiteSpace($Token)) {
    $Token = "harbor-local-embedding-token"
}
$env:HARBOR_LOCAL_EMBEDDING_PATH = $resolvedModelDir
$env:HARBOR_EMBEDDING_GATEWAY_TOKEN = $Token

if ($ValidateOnly) {
    [ordered]@{
        status = "ok"
        python = $pythonExecutable
        model_path = $resolvedModelDir
        host = $HostAddress
        port = $Port
        device = $Device
    } | ConvertTo-Json -Compress
    return
}

& $pythonExecutable (Join-Path $projectRoot "local_embedding_service\server.py") --model-dir $resolvedModelDir --host $HostAddress --port $Port --device $Device --token $Token
