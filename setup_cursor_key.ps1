# 一次性配置 CURSOR_API_KEY
# 用法：在 PowerShell 运行  .\setup_cursor_key.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "=== TradePilot Cursor AI 配置 ===" -ForegroundColor Cyan
Write-Host "1. 浏览器将打开 Cursor Cloud Agents 页面"
Write-Host "2. 点击 Create API Key，复制以 cursor_ 开头的密钥"
Write-Host ""

Start-Process "https://cursor.com/dashboard/cloud-agents"
Start-Sleep -Seconds 2

$key = Read-Host "请粘贴 CURSOR_API_KEY"
$key = $key.Trim()
if (-not ($key.StartsWith("cursor_") -or $key.StartsWith("crsr_"))) {
    Write-Host "警告：密钥通常以 cursor_ 或 crsr_ 开头，请确认是否粘贴正确" -ForegroundColor Yellow
}

# 写入用户环境变量（永久）
[Environment]::SetEnvironmentVariable("CURSOR_API_KEY", $key, "User")
$env:CURSOR_API_KEY = $key

# 写入 secrets.env（TradePilot.exe 从 dist 运行时也会读取）
$paths = @(
    Join-Path $root "secrets.env",
    Join-Path $root "logs\secrets.env",
    Join-Path $root "dist\logs\secrets.env"
)
$content = "CURSOR_API_KEY=$key`n"
foreach ($p in $paths) {
    $dir = Split-Path $p -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Set-Content -Path $p -Value $content -Encoding UTF8
    Write-Host "已写入: $p" -ForegroundColor Green
}

Write-Host ""
Write-Host "配置完成！请重启 TradePilot.exe" -ForegroundColor Green
Write-Host ""
