# install_refresh_task.ps1 —— 注册「账号自动续期」计划任务
#
# 用法（PowerShell，无需管理员，仅当前用户）：
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Hours 8      # 每 8 小时一次
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Trae         # 注册 Trae 侧
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Remove       # 移除任务
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Trae -Remove
#
# 为什么需要它：现在登录流程固定走一次「切换/绑定」，新登账号一律是
# enterprise_switch 通道（access 只有 30 天，退出重登也改不回来）。
# 好在 refresh 会滚动 refreshToken（重置为新的 60 天），
# 只要在 access 到期前定时跑 --refresh-all 就不会掉线。

param(
    [switch]$Trae,
    [switch]$Remove,
    [int]$Hours = 12
)

$ErrorActionPreference = 'Stop'

if ($Trae) {
    $taskName = 'WB-Switcher-Trae-RefreshAll'
    $scriptName = 'trae_refresh_all.cmd'
} else {
    $taskName = 'WB-Switcher-RefreshAll'
    $scriptName = 'refresh_all.cmd'
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root $scriptName
if (-not (Test-Path $target)) {
    Write-Host "找不到脚本：$target" -ForegroundColor Red
    exit 1
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除计划任务：$taskName" -ForegroundColor Yellow
    exit 0
}

# /nopause：计划任务是非交互环境，脚本末尾的 pause 必须跳过，否则任务会一直挂住
$action = New-ScheduledTaskAction -Execute $target -Argument '/nopause' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger `
    -Once -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval (New-TimeSpan -Hours $Hours) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "账号切换器：每 $Hours 小时对账号库全量续期一次" `
    -Force | Out-Null

Write-Host "已注册计划任务：$taskName（每 $Hours 小时执行 $scriptName）" -ForegroundColor Green
Write-Host "查看：Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo" -ForegroundColor Gray
