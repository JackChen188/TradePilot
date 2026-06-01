# 只保留一个 TradePilot + 清理僵死的 clawbot 桥接，再启动 exe
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$exe = Join-Path $root "dist\TradePilot.exe"
$lock = Join-Path $root "dist\logs\clawbot_bridge.lock"

Write-Host "==> 结束多余的 TradePilot.exe（保留最新启动的一个）"
$tp = Get-Process -Name TradePilot -ErrorAction SilentlyContinue | Sort-Object StartTime
if ($tp.Count -gt 1) {
  $tp[0..($tp.Count - 2)] | ForEach-Object {
    Write-Host "  结束 PID $($_.Id) (启动于 $($_.StartTime))"
    Stop-Process -Id $_.Id -Force
  }
}

if (Test-Path $lock) {
  $pid = [int](Get-Content $lock -Raw).Trim()
  $alive = $false
  if ($pid -gt 0) {
    try { Get-Process -Id $pid -ErrorAction Stop | Out-Null; $alive = $true } catch {}
  }
  if (-not $alive) {
    Write-Host "==> 删除过期桥接锁 $lock"
    Remove-Item $lock -Force
  } else {
    Write-Host "==> 桥接进程仍在运行 pid=$pid（TradePilot 重启后会复用或替换）"
  }
}

if (-not (Test-Path $exe)) {
  Write-Error "未找到 $exe ，请先 pyinstaller TradePilot.spec"
}
Write-Host "==> 启动 $exe"
Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
Write-Host "完成。请在微信 ClawBot 发「测试」或「帮助」验证。"
