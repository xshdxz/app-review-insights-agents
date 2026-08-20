# 本机一键初始化：venv + 依赖 + .env 模板
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".\.venv\Scripts\python" -m pip install --upgrade pip
& ".\.venv\Scripts\python" -m pip install -e ".[dev]"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已生成 .env，请填写 DEEPSEEK_API_KEY（或 MODEL_API_KEY）后继续。"
} else {
    Write-Host ".env 已存在，跳过。"
}
Write-Host "完成。启动：.\.venv\Scripts\streamlit run app.py"
