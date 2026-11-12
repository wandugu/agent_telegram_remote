<#
Register the Telegram bridge as a hidden logon task, show its state, or remove it.

    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Action Install
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Action Status
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Action Remove

Run it from a normal PowerShell, not an elevated one (an SSH session counts as
elevated): a task registered from an elevated shell belongs to Administrators
and can no longer be changed by you. Create .venv first with `uv sync`.
After moving this folder or unpacking a new version, run `uv sync`, then
Install, then `python -m agent_telegram hooks install` again (both store the
folder's absolute path). Install restarts the bridge once no confirmation
or send is pending (an open approval card does not hold it up: Claude's own
dialog stays usable, and the restart marks the card).
#>
param(
    [ValidateSet('Install', 'Status', 'Remove')][string]$Action = 'Status',
    [string]$TaskName = 'AgentTelegram-Bridge',
    # Install waits this long for pending confirmations and sends before restarting a running bridge.
    [int]$WaitIdleSeconds = 600
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'  # matches [Console]::OutputEncoding above
$package = $PSScriptRoot
$pythonw = Join-Path $package '.venv\Scripts\pythonw.exe'
$python = Join-Path $package '.venv\Scripts\python.exe'
$arguments = '-m agent_telegram.tgbridge run'
$varDir = if ($env:AGENT_TG_VAR_DIR) { $env:AGENT_TG_VAR_DIR } else { Join-Path $env:LOCALAPPDATA 'agent-telegram' }

# Port from tgbridge\config.json (hook_port), 8767 by default.
function Get-HookPort {
    $configPath = Join-Path $varDir 'tgbridge\config.json'
    if (Test-Path $configPath -PathType Leaf) {
        try { $port = (Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).hook_port; if ($port) { return [int]$port } } catch { }
    }
    return 8767
}
$statusUrl = "http://127.0.0.1:$(Get-HookPort)/status"

function Get-BridgeStatus {
    try {
        $status = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 5
        if ($status.service -eq 'tgbridge') { return $status }
    } catch { }
    return $null
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# What a restart would cut off; '' when idle or not running.
function Get-BridgeBusy {
    $status = Get-BridgeStatus
    if ($null -eq $status) { return '' }
    $busy = @()
    if ($status.deliveries -gt 0) { $busy += "deliveries=$($status.deliveries)" }
    if ($status.pending_confirmations -gt 0) { $busy += "pending_confirmations=$($status.pending_confirmations)" }
    # A confirmed write or a Codex answer between the tap and its tracked delivery.
    if ($status.executing -gt 0) { $busy += "executing=$($status.executing)" }
    if ($status.outbox -gt 0) { $busy += "outbox=$($status.outbox)" }
    return ($busy -join ' ')
}

function Wait-BridgeIdle([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $announced = $false
    while ($true) {
        $busy = Get-BridgeBusy
        if (-not $busy) { return '' }
        if ((Get-Date) -ge $deadline) { return $busy }
        if (-not $announced) { Write-Host "Waiting up to ${Seconds}s for the bridge to go idle ($busy)"; $announced = $true }
        Start-Sleep -Seconds 2
    }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $Action -ne 'Status') {
    if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Arguments -ne $arguments) {
        throw "Refusing to modify an unrelated task named $TaskName; pass -TaskName to use another name."
    }
}
if ($Action -eq 'Remove') {
    # Hooks pointing at a stopped bridge would add about 2 s to every Claude tool call and leave the
    # port to whatever binds it next, so they go first; keep the bridge if that fails.
    if (Test-Path $python -PathType Leaf) {
        $ErrorActionPreference = 'Continue'
        & $python -m agent_telegram.tgbridge hooks remove
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -ne 0) { throw "hooks remove failed (exit $code); the bridge was left running. Fix it and rerun -Action Remove." }
    } else {
        Write-Warning '.venv missing: remove the agent_telegram hooks from ~/.claude/settings.json by hand.'
    }
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Write-Output "Bridge task removed; token, config, state and logs kept in $varDir."
    exit
}
if ($Action -eq 'Install') {
    if (-not (Test-Path $pythonw -PathType Leaf)) { throw "$pythonw not found: run 'uv sync' in $package first." }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning "This PowerShell is elevated: $TaskName will belong to Administrators. Prefer a normal PowerShell."
    }
    $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $package
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    # Interactive: the bridge reads your Codex/Claude state and keyboard idle time.
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description "agent-telegram: Telegram bridge for local Codex and Claude Code conversations; logs in $varDir\logs." -Force -ErrorAction Stop | Out-Null
    } catch [Microsoft.Management.Infrastructure.CimException] {
        throw "Cannot replace ${TaskName}: it was registered from an elevated shell. Run -Action Remove once from an administrator PowerShell, then -Action Install from a normal one. $($_.Exception.Message)"
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        $busy = Wait-BridgeIdle $WaitIdleSeconds
        if ($busy) {
            Write-Output "Task updated but the running bridge was left alone ($busy); rerun Install when it is idle."
        } else {
            Stop-ScheduledTask -TaskName $TaskName
            [void](Wait-Until { $null -eq (Get-BridgeStatus) } 15)
            [void](Wait-Until { (Get-ScheduledTask -TaskName $TaskName).State -ne 'Running' } 15)
            Start-ScheduledTask -TaskName $TaskName
        }
    } else {
        Start-ScheduledTask -TaskName $TaskName
    }
    if (-not (Wait-Until { $null -ne (Get-BridgeStatus) } 45)) {
        Write-Warning "The bridge did not answer at $statusUrl yet; check $varDir\logs\bridge.log (token set? bound? proxy running?)."
    }
}
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "$TaskName is not installed. Run with -Action Install."
    exit
}
$task | Select-Object TaskName,State | Format-Table -AutoSize | Out-String -Width 200
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime,LastTaskResult | Format-Table -AutoSize | Out-String -Width 200
if (Test-Path $python -PathType Leaf) {
    $ErrorActionPreference = 'Continue'
    & $python -m agent_telegram.tgbridge status
}
