param(
    [string]$Python = $env:HARBOR_PYTHON,
    [string]$ModelPath = $env:HARBOR_LOCAL_MODEL_PATH,
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8091,
    [string]$Device = $env:HARBOR_LOCAL_MODEL_DEVICE,
    [string]$Token = $env:HARBOR_MODEL_GATEWAY_TOKEN,
    [switch]$ValidateOnly,
    [switch]$Eager
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$gateway = Join-Path $projectRoot "local_model_service\server.py"

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

if ([string]::IsNullOrWhiteSpace($ModelPath)) {
    $ModelPath = Join-Path $projectRoot ".models\Qwen3-VL-8B-Instruct-int4-ov"
} elseif (-not [System.IO.Path]::IsPathRooted($ModelPath)) {
    $ModelPath = Join-Path $projectRoot $ModelPath
}
if (-not (Test-Path -LiteralPath $ModelPath -PathType Container)) {
    throw "Local Qwen model not found: '$ModelPath'. Pass -ModelPath, set HARBOR_LOCAL_MODEL_PATH, or place it under '.models\Qwen3-VL-8B-Instruct-int4-ov'."
}
$resolvedModelPath = (Resolve-Path -LiteralPath $ModelPath).Path

$env:HARBOR_LOCAL_MODEL_PATH = $resolvedModelPath
if ([string]::IsNullOrWhiteSpace($Token)) {
    $Token = "harbor-local-model-token"
}
$env:HARBOR_MODEL_GATEWAY_TOKEN = $Token

if ($ValidateOnly) {
    [ordered]@{
        status = "ok"
        python = $pythonExecutable
        model_path = $resolvedModelPath
        host = $HostAddress
        port = $Port
        device = $Device
    } | ConvertTo-Json -Compress
    return
}

$arguments = @(
    $gateway,
    "--model-path", $resolvedModelPath,
    "--host", $HostAddress,
    "--port", "$Port",
    "--device", $Device
)
if ($Eager) { $arguments += "--eager" }

& $pythonExecutable @arguments
