# Docker 一键部署：构建并启动 web + worker
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".env")) {
    throw "缺少 .env，请先运行 .\scripts\setup.ps1 或复制 .env.example 为 .env"
}

docker compose build
docker compose up -d
Start-Sleep -Seconds 5
docker compose ps
Write-Host "Web: http://localhost:8501"
