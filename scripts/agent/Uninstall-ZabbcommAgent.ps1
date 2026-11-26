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
