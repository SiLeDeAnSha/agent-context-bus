<#
.SYNOPSIS
  Install Windows daily collector & offline cache sync scheduled task for Daily Memory Hub (fnOS NAS).
.EXAMPLE
  .\setup_windows_collector.ps1 -MachineId "win-desktop" -TriggerTime "08:55"
#>
param(
  [string]$MachineId = $env:COMPUTERNAME.ToLower(),
  [string]$TriggerTime = "08:55",
  [string]$PythonExe = "python",
  [string]$RemoteConfig = "$env:USERPROFILE\.daily-memory\remote.json"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BaseDir = "$env:USERPROFILE\.daily-memory"
$LogDir = "$BaseDir\logs"

New-Item -ItemType Directory -Force -Path $BaseDir, $LogDir | Out-Null

if (-not (Test-Path $RemoteConfig)) {
  Copy-Item "$ScriptDir\remote.example.json" $RemoteConfig
  Write-Host "Created template $RemoteConfig - please fill in your NAS endpoints and tokens."
}

$RunnerPath = "$BaseDir\run_collector.ps1"
$RunnerContent = @"
`$ErrorActionPreference = "Stop"
`$log = "$LogDir\collector.log"
`$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path `$log -Value "=== [`$ts] Start collect+push ($MachineId) ==="
& "$PythonExe" "$ProjectDir\collect.py" --yesterday --missing --machine "$MachineId" --push --remote-config "$RemoteConfig" *>> `$log
& "$PythonExe" "$ProjectDir\query_memory.py" --sync-cache --remote-config "$RemoteConfig" *>> `$log
"@
Set-Content -Path $RunnerPath -Value $RunnerContent -Encoding UTF8

$TaskName = "DailyMemoryCollector"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Installed Scheduled Task '$TaskName' (daily $TriggerTime, machine=$MachineId)."
Write-Host "Test now with: powershell -ExecutionPolicy Bypass -File `"$RunnerPath`""
