# Bump this whenever the script changes behaviour.
$AgentVersion = '1.2.0'

$ProgressPreference = 'SilentlyContinue'

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

$org = "$orgName".Trim()
$device = "$assetName".Trim()
$body = @{ org = $org; device = $device; agent_version = $AgentVersion }

# If the type is unknown, leave is_server out so zabbcomm keeps what it already knows.
$type = "$deviceType".Trim()
$typeIsValid = @('server', 'user') -contains $type
if ($typeIsValid) {
    $body.is_server = ($type -eq 'server')
}
else {
    Write-Output "zabbcomm: deviceType must be 'server' or 'user', got '$type'"
}

$json = $body | ConvertTo-Json -Compress

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $request = @{
        Method             = 'Post'
        Uri                = "$baseUrl/api/v1/checkin"
        Headers            = @{ Authorization = "Bearer $("$apiToken".Trim())" }
        ContentType        = 'application/json; charset=utf-8'
        # Send bytes: Windows PowerShell would otherwise mangle non-ASCII names.
        Body               = [Text.Encoding]::UTF8.GetBytes($json)
        TimeoutSec         = 15
        MaximumRedirection = 0
        UseBasicParsing    = $true
        ErrorAction        = 'Stop'
    }
    $response = Invoke-WebRequest @request
}
catch {
    $statusCode = 0
    if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }

    # The server answered and refused: wrong token, registration limit, or a bad name.
    if (@(401, 403, 422) -contains $statusCode) {
        Write-Output "zabbcomm: check-in rejected for '$org' / '$device' (HTTP $statusCode)"
        exit 1
    }

    $reason = $_.Exception.Message
    # Windows PowerShell reports a refused redirect with a message that doesn't say so.
    if ($_.FullyQualifiedErrorId -like 'MaximumRedirectExceeded*') {
        $reason = 'unexpected redirect (captive portal, or wrong appUrl)'
    }

    # No connection, timeout, server error: Zabbix does the alerting, so don't fail here.
    Write-Output "zabbcomm: check-in failed: $reason"
    exit 0
}

# Anything but 204 didn't come from zabbcomm (for example a captive portal page).
if ($response.StatusCode -ne 204) {
    Write-Output "zabbcomm: check-in not confirmed (HTTP $($response.StatusCode))"
    exit 0
}

Write-Output "zabbcomm: checked in '$org' / '$device'"
if (-not $typeIsValid) { exit 1 }
exit 0
