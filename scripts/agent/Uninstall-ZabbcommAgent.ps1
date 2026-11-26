<#
.SYNOPSIS
    Removes the zabbcomm check-in agent from this device.

.DESCRIPTION
    Removes the scheduled task and the agent folder, including its stored token. Safe to
    run when nothing is installed. Remove Install-ZabbcommAgent.ps1 from the device's
    Syncro policy first, or its next run installs the agent again.

    The device stays in zabbcomm until DEVICE_FORGET_AFTER and reads as offline until
    then. Add it to deny.json on the server to drop it straight away.

    Written for Windows PowerShell 5.1, run as SYSTEM. Needs no Syncro variables.

    Exit codes:
      0  The agent is gone
      1  Something couldn't be removed
#>

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
    Write-Output 'zabbcomm: the ProgramData environment variable is empty'
    exit 1
}
$InstallDir = Join-Path $env:ProgramData 'zabbcomm'
$TaskPath = '\zabbcomm\'
$TaskName = 'Checkin'

function Get-AgentTask {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

# Deletes a file or folder. A link is removed itself, never what it points to (which
# Remove-Item -Recurse can't be trusted to respect, running as SYSTEM).
function Remove-Tree([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        Remove-Item -LiteralPath $Path -Force
        return
    }
    if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force) { Remove-Tree $child.FullName }
    }
    [IO.Directory]::Delete($Path)
}

$failures = @()

# The task goes first, so it can't run while its files are being removed.
try {
    if (Get-AgentTask) {
        Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
    }
}
catch { $failures += "scheduled task: $($_.Exception.Message)" }

try {
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $scheduler.GetFolder('\').DeleteFolder('zabbcomm', 0)
}
catch { Write-Verbose "Task folder not removed: $($_.Exception.Message)" }

try {
    if (Get-Item -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue) {
        Remove-Tree $InstallDir
    }
}
catch { $failures += "agent folder: $($_.Exception.Message)" }

if (Get-AgentTask) { $failures += 'the scheduled task still exists' }
if (Test-Path -LiteralPath $InstallDir) { $failures += "$InstallDir still exists" }

if ($failures) {
    Write-Output "zabbcomm: uninstall incomplete: $($failures -join '; ')"
    exit 1
}
Write-Output 'zabbcomm: agent removed'
exit 0
