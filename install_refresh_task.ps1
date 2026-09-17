# install_refresh_task.ps1 —— 注册「账号自动续期」计划任务
#
# 用法（PowerShell，无需管理员，仅当前用户）：
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -At 06:00   # 改每日时刻
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Hours 12   # 改成每 12 小时
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Trae        # 注册 Trae 侧
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Remove      # 移除任务
#   powershell -ExecutionPolicy Bypass -File install_refresh_task.ps1 -Trae -Remove
#
# 默认：每天 07:30 跑一次 refresh_all.cmd（对齐上级「自动签到」项目的旧规则：
#   checkin_task.xml 每天 00:01、refresh_task.xml 每天 07:30）。
#
# 为什么每天一次就够：任务里跑的 --refresh-all **默认走门卫**（force=False），
# 由 workbuddy_checkin.refresh_account 判断 —— 只有 accessToken 剩余 < 3 天
# 且距上次实际续期 ≥ 24 小时才真正刷新，否则直接跳过、不写任何文件。
# 所以「每天跑一次」的日常开销只有几次本地读取；真正刷新只发生在快到期时。
# 与上级 refresh_guard.py / --refresh 的门卫语义一致。
#
# 为什么需要它：现在登录流程固定走一次「切换/绑定」，新登账号一律是
# enterprise_switch 通道（access 只有 30 天，退出重登也改不回来）。
# 好在 refresh 会滚动 refreshToken（重置为新的 60 天），
# 只要在 access 到期前续一次就不会掉线。
#
# 本机注意：PowerShell ExecutionPolicy 禁止直接运行 .ps1，
# 需要 `-ExecutionPolicy Bypass`，或把脚本内容读成 scriptblock 执行
# （后者会让 $MyInvocation.MyCommand.Path 为空，要手工补 $root）。

param(
    [switch]$Trae,
    [switch]$Remove,
    [string]$At = '07:30',
    [int]$Hours = 0
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
if (-not $root) { $root = (Get-Location).Path }
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

if ($Hours -gt 0) {
    $trigger = New-ScheduledTaskTrigger `
        -Once -At ((Get-Date).AddMinutes(2)) `
        -RepetitionInterval (New-TimeSpan -Hours $Hours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $when = "每 $Hours 小时"
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]$At)
    $when = "每天 $At"
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "账号切换器：$when 对账号库跑一遍续期（默认走门卫，只续快到期的）" `
    -Force | Out-Null

Write-Host "已注册计划任务：$taskName（$when 执行 $scriptName）" -ForegroundColor Green
Write-Host "查看：Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo" -ForegroundColor Gray
