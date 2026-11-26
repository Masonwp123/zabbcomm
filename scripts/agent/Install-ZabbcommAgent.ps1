# Bump this whenever either part of the agent changes behaviour.
$AgentVersion = '2.1.0'
$IntervalMinutes = 5

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
    Write-Output 'zabbcomm: the ProgramData environment variable is empty'
    exit 1
}

$InstallDir = Join-Path $env:ProgramData 'zabbcomm'
$AgentPath = Join-Path $InstallDir 'Invoke-ZabbcommAgent.ps1'
$ConfigPath = Join-Path $InstallDir 'config.json'
$StatusPath = Join-Path $InstallDir 'status.json'
$PingPath = Join-Path $InstallDir 'ping.txt'
$TaskPath = '\zabbcomm\'
$TaskName = 'Checkin'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$TaskArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$AgentPath`""

# Owner Administrators; full control for SYSTEM and Administrators only; no inheritance.
$LockedSddl = 'O:BAG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
$TrustedSids = @('S-1-5-18', 'S-1-5-32-544')

# ---------------------------------------------------------------------------- folder

# True if the install folder is a real folder that only SYSTEM and Administrators control.
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

function Initialize-InstallDirectory {
    # Any user can create folders in ProgramData. One that isn't locked down must not
    # receive the token, or a script that runs as SYSTEM, so start again from nothing.
    if (-not (Test-LockedDirectory)) {
        if (Get-Item -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue) {
            Remove-Tree $InstallDir
        }
        $security = New-Object Security.AccessControl.DirectorySecurity
        $security.SetSecurityDescriptorSddlForm($LockedSddl)
        [void][IO.Directory]::CreateDirectory($InstallDir, $security)
    }
    if (-not (Test-LockedDirectory)) {
        throw "$InstallDir could not be locked down"
    }
}

# Tells the agent that this script still reaches the device: a new value restarts the
# agent's expiry counters. The agent may be reading the file, so retry briefly.
function Send-Ping {
    foreach ($attempt in 1..5) {
        try {
            [IO.File]::WriteAllText($PingPath, [guid]::NewGuid().ToString())
            return
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

# Ping an installed agent before checking anything else, so a mistake in a Syncro
# variable can't make working agents expire.
try {
    if (Test-LockedDirectory) { Send-Ping }
}
catch {
    Write-Output "zabbcomm: could not ping the agent: $($_.Exception.Message)"
}

# ------------------------------------------------------------------------- variables

$missing = @('orgName', 'assetName', 'appUrl', 'apiToken') | Where-Object {
    [string]::IsNullOrWhiteSpace((Get-Variable -Name $_ -ValueOnly -ErrorAction SilentlyContinue))
}
if ($missing) {
    Write-Output "zabbcomm: missing Syncro variable(s): $($missing -join ', ')"
    exit 1
}

$baseUrl = "$appUrl".Trim().TrimEnd('/')
# Plain HTTP would expose the token, so it's only allowed for testing on this machine.
if ($baseUrl -notmatch '^https://' -and $baseUrl -notmatch '^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)') {
    Write-Output 'zabbcomm: appUrl must start with https://'
    exit 1
}

# A bad optional value is reported (exit 1) but doesn't stop the install.
$inputsValid = $true
$expiry = [ordered]@{ expireAfterOnlineDays = 7; expireAfterRunningDays = 30 }
foreach ($name in @($expiry.Keys)) {
    $text = "$(Get-Variable -Name $name -ValueOnly -ErrorAction SilentlyContinue)".Trim()
    if (-not $text) { continue }
    $days = 0
    if ([int]::TryParse($text, [ref]$days) -and $days -ge 0) {
        $expiry[$name] = $days
        if ($days -in 1, 2) {
            Write-Output "zabbcomm: warning: $name = $days leaves little room for a missed Syncro run"
        }
    }
    else {
        Write-Output "zabbcomm: $name must be a whole number of days (0 turns it off), got '$text'; using $($expiry[$name])"
        $inputsValid = $false
    }
}

# ---------------------------------------------------------------- the installed agent

# The script the scheduled task runs. It is written to the device by this installer.
$AgentScript = @'
# zabbcomm agent: sends one check-in. Written by Install-ZabbcommAgent.ps1, which
# overwrites any edits made here.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$InstallDir = Join-Path $env:ProgramData 'zabbcomm'
$ConfigPath = Join-Path $InstallDir 'config.json'
$StatusPath = Join-Path $InstallDir 'status.json'
$PingPath = Join-Path $InstallDir 'ping.txt'
$UsagePath = Join-Path $InstallDir 'usage.json'
$TaskPath = '\zabbcomm\'
$TaskName = 'Checkin'

$config = [IO.File]::ReadAllText($ConfigPath) | ConvertFrom-Json

function Write-Status([string]$Result, [string]$Detail) {
    $status = [ordered]@{
        last_run = [DateTime]::UtcNow.ToString('o')
        result   = $Result
        detail   = $Detail
        version  = $config.version
    }
    [IO.File]::WriteAllText($StatusPath, ($status | ConvertTo-Json))
}

# True only for an uninstall order signed with our token for this exact request, so a
# captive portal, web filter or replayed reply can't remove the agent.
function Test-UninstallOrder([string]$Signature, [string]$Nonce) {
    $hmac = New-Object Security.Cryptography.HMACSHA256
    $hmac.Key = [Text.Encoding]::UTF8.GetBytes($config.token)
    $hash = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("uninstall:$Nonce"))
    $expected = -join ($hash | ForEach-Object { $_.ToString('x2') })
    return $Signature -ceq $expected
}

function Remove-Agent {
    # Files first: if removing the task ends this process, nothing is left behind.
    try { [IO.Directory]::Delete($InstallDir, $true) }
    catch { Write-Output "zabbcomm: could not delete ${InstallDir}: $($_.Exception.Message)" }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    try {
        $scheduler = New-Object -ComObject Schedule.Service
        $scheduler.Connect()
        $scheduler.GetFolder('\').DeleteFolder('zabbcomm', 0)
    }
    catch { Write-Verbose "Task folder not removed: $($_.Exception.Message)" }
}

# Counts this run, and says whether the Syncro installer has been silent for too long.
# Runs are counted instead of comparing dates, so time powered off never counts and a
# wrong clock doesn't matter. A new ping from the installer restarts both counts.
function Test-Expired([bool]$Online) {
    $usage = $null
    try { $usage = [IO.File]::ReadAllText($UsagePath) | ConvertFrom-Json }
    catch { Write-Verbose 'Starting new counters' }
    try { $ping = [IO.File]::ReadAllText($PingPath) }
    catch { $ping = "$($usage.ping)" }

    $onlineRuns = 0
    $runningRuns = 0
    if ($usage -and $usage.ping -ceq $ping) {
        $onlineRuns = [int]$usage.online_runs
        $runningRuns = [int]$usage.running_runs
    }
    $runningRuns++
    if ($Online) { $onlineRuns++ }
    $counts = [ordered]@{ ping = $ping; online_runs = $onlineRuns; running_runs = $runningRuns }
    [IO.File]::WriteAllText($UsagePath, ($counts | ConvertTo-Json))

    $runsPerDay = 1440 / $config.interval_minutes
    return ($config.expire_online_days -gt 0 -and $onlineRuns -ge $config.expire_online_days * $runsPerDay) -or
        ($config.expire_running_days -gt 0 -and $runningRuns -ge $config.expire_running_days * $runsPerDay)
}

# Ends a run: record the result, then remove the agent if it has expired.
function Complete-Run([string]$Result, [string]$Detail, [int]$ExitCode) {
    Write-Status $Result $Detail
    # "Online" means zabbcomm itself accepted the check-in.
    if (Test-Expired ($Result -eq 'ok')) {
        Write-Output 'zabbcomm: no contact from the Syncro installer for too long; removing the agent'
        Remove-Agent
        exit 0
    }
    exit $ExitCode
}

$body = @{ org = $config.org; device = $config.device; agent_version = $config.version }
if ($null -ne $config.is_server) { $body.is_server = [bool]$config.is_server }

$random = New-Object byte[] 16
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($random)
$nonce = -join ($random | ForEach-Object { $_.ToString('x2') })

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $request = @{
        Method             = 'Post'
        Uri                = "$($config.app_url)/api/v1/checkin"
        Headers            = @{
            Authorization      = "Bearer $($config.token)"
            'X-Zabbcomm-Nonce' = $nonce
        }
        ContentType        = 'application/json; charset=utf-8'
        Body               = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
        TimeoutSec         = 15
        MaximumRedirection = 0
        UseBasicParsing    = $true
    }
    $response = Invoke-WebRequest @request
}
catch {
    $reply = $_.Exception.Response
    $statusCode = 0
    if ($reply) { $statusCode = [int]$reply.StatusCode }

    if ($statusCode -eq 410 -and (Test-UninstallOrder $reply.Headers['X-Zabbcomm-Uninstall'] $nonce)) {
        Write-Output 'zabbcomm: the server denies this device; removing the agent'
        Remove-Agent
        exit 0
    }
    if (@(401, 403, 422) -contains $statusCode) { Complete-Run 'rejected' "HTTP $statusCode" 1 }
    Complete-Run 'unreachable' $_.Exception.Message 0
}

if ($response.StatusCode -ne 204) { Complete-Run 'not confirmed' "HTTP $($response.StatusCode)" 0 }
Complete-Run 'ok' '' 0
'@

$settings = [ordered]@{
    app_url             = $baseUrl
    token               = "$apiToken".Trim()
    org                 = "$orgName".Trim()
    device              = "$assetName".Trim()
    version             = $AgentVersion
    interval_minutes    = $IntervalMinutes
    expire_online_days  = $expiry.expireAfterOnlineDays
    expire_running_days = $expiry.expireAfterRunningDays
}
# If the type is unknown, leave is_server out so zabbcomm keeps what it already knows.
$type = "$deviceType".Trim()
if (@('server', 'user') -contains $type) {
    $settings.is_server = ($type -eq 'server')
}
else {
    Write-Output "zabbcomm: deviceType must be 'server' or 'user', got '$type'"
    $inputsValid = $false
}
$ConfigJson = $settings | ConvertTo-Json

# ------------------------------------------------------------------- task and health

function Register-AgentTask {
    $action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $TaskArguments
    $repeating = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $atStartup = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    # Laptops on battery must keep reporting, or they look offline.
    $options = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action `
        -Trigger $repeating, $atStartup -Principal $principal -Settings $options `
        -Description 'Sends zabbcomm check-ins.' -Force | Out-Null
}

function Get-TaskProblem {
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return 'scheduled task is missing' }
    if (-not $task.Settings.Enabled -or "$($task.State)" -eq 'Disabled') { 'scheduled task is disabled' }
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or $actions[0].Execute -ne $PowerShellExe -or $actions[0].Arguments -cne $TaskArguments) {
        'task action has changed'
    }
    $onSchedule = @($task.Triggers) |
        Where-Object { $_.Enabled -and $_.Repetition.Interval -eq "PT$($IntervalMinutes)M" }
    if (-not $onSchedule) { 'task schedule has changed' }
    if (@('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -notcontains $task.Principal.UserId) {
        'task does not run as SYSTEM'
    }
    if ($task.Settings.DisallowStartIfOnBatteries -or $task.Settings.StopIfGoingOnBatteries) {
        'task stops on battery power'
    }
}

function Get-FileText([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return [IO.File]::ReadAllText($Path) }
}

function Get-AgentStatus {
    try { return Get-FileText $StatusPath | ConvertFrom-Json }
    catch { return $null }
}

# Everything that is wrong with the installed agent; nothing if it's healthy.
function Get-AgentProblem {
    if (-not (Test-LockedDirectory)) { return 'agent folder is missing or not locked down' }
    if ((Get-FileText $AgentPath) -cne $AgentScript) { 'agent script is missing or outdated' }
    if ((Get-FileText $ConfigPath) -cne $ConfigJson) { 'configuration is missing or outdated' }
    Get-TaskProblem

    $status = Get-AgentStatus
    if (-not $status) { return 'agent has not run' }
    $lastRun = [DateTime]::Parse($status.last_run, [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    # Three missed runs.
    if (([DateTime]::UtcNow - $lastRun).TotalMinutes -gt 3 * $IntervalMinutes) {
        "agent has not run since $($status.last_run)"
    }
}

function Complete-Install {
    $status = Get-AgentStatus
    if ($status.result -eq 'rejected') {
        Write-Output "zabbcomm: agent is running, but check-ins are rejected ($($status.detail))"
        exit 1
    }
    Write-Output "zabbcomm: agent healthy; last check-in: $($status.result) at $($status.last_run)"
    if (-not $inputsValid) { exit 1 }
    exit 0
}

# ------------------------------------------------------------------------------- run

try {
    $problems = @(Get-AgentProblem)
    if (-not $problems) { Complete-Install }

    Write-Output "zabbcomm: installing or repairing: $($problems -join '; ')"
    Initialize-InstallDirectory
    Send-Ping
    [IO.File]::WriteAllText($AgentPath, $AgentScript)
    [IO.File]::WriteAllText($ConfigPath, $ConfigJson)
    if (Test-Path -LiteralPath $StatusPath) { Remove-Item -LiteralPath $StatusPath -Force }
    Register-AgentTask
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName

    # Wait for the first run to report, or to remove the agent.
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and (Test-Path -LiteralPath $InstallDir) -and -not (Get-AgentStatus)) {
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        Write-Output 'zabbcomm: the server denies this device, so the agent removed itself'
        exit 0
    }

    $problems = @(Get-AgentProblem)
    if ($problems) {
        Write-Output "zabbcomm: agent is not healthy: $($problems -join '; ')"
        exit 1
    }
    Complete-Install
}
catch {
    Write-Output "zabbcomm: install failed: $($_.Exception.Message)"
    exit 1
}
