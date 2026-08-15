[CmdletBinding()]
param(
    [switch]$Live,
    [string]$Dataset = "evals/gold-reviews.json",
    [string]$Output
)

$ErrorActionPreference = "Stop"
$utf8Encoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runnerPath = Join-Path $projectRoot "scripts\run_eval.py"

function ConvertFrom-Utf8Base64 {
    param([string]$Value)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $message = ConvertFrom-Utf8Base64 "5pyq5om+5Yiw6aG555uu6Jma5ouf546v5aKD44CC6K+35YWI5oyJ54WnIFJFQURNRSDliJvlu7ogLnZlbnYg5bm25a6J6KOF5L6d6LWW44CC"
    Write-Error ($message + [Environment]::NewLine + $pythonPath)
    exit 2
}

if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    $message = ConvertFrom-Utf8Base64 "5pyq5om+5YiwIFByb21wdCDor4TmtYvohJrmnKzjgILor7fnoa7orqTpobnnm67mlofku7blrozmlbTjgII="
    Write-Error ($message + [Environment]::NewLine + $runnerPath)
    exit 3
}

$arguments = @($runnerPath, "--dataset", $Dataset)
if ($Live) {
    $arguments += "--live"
}
if ($Output) {
    $arguments += @("--output", $Output)
}

Push-Location -LiteralPath $projectRoot
$previousPythonUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = "1"
    & $pythonPath @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
    Pop-Location
}

exit $exitCode
