$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
    Write-Output 'zabbcomm: the ProgramData environment variable is empty'
    exit 1
}
$InstallDir = Join-Path $env:ProgramData 'zabbcomm'
$TaskPath = '\zabbcomm\'
$TaskName = 'Checkin'
$TrustedSids = @('S-1-5-18', 'S-1-5-32-544')

function Get-AgentTask {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

# True if the install folder is a real folder that only SYSTEM and Administrators control,
# as the installer creates it. Only then is it safe to look inside and delete it.
function Test-LockedDirectory {
    $item = Get-Item -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.PSIsContainer) { return $false }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
    $acl = Get-Acl -LiteralPath $InstallDir
    $sidType = [Security.Principal.SecurityIdentifier]
    if ($TrustedSids -notcontains $acl.GetOwner($sidType).Value) { return $false }
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $others = $acl.GetAccessRules($true, $true, $sidType) |
        Where-Object { $TrustedSids -notcontains $_.IdentityReference.Value }
    return -not $others
}

# Deletes a folder only trusted accounts can change. A link inside it is removed itself,
# never what it points to.
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

# Renames a file, folder or link out of the way without looking inside it. A folder
# someone else controls must never be walked or deleted: they could swap part of it for
# a link mid-delete and make this script, running as SYSTEM, delete files elsewhere.
function Move-Aside([string]$Path) {
    $aside = "$Path.untrusted-$([guid]::NewGuid().ToString('N'))"
    if ((Get-Item -LiteralPath $Path -Force).PSIsContainer) { [IO.Directory]::Move($Path, $aside) }
    else { [IO.File]::Move($Path, $aside) }
    return $aside
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
    if (Test-LockedDirectory) {
        Remove-Tree $InstallDir
    }
    elseif (Get-Item -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue) {
        $aside = Move-Aside $InstallDir
        $failures += "$InstallDir wasn't locked down, so it was moved unopened to $aside. Check it, then delete it"
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
