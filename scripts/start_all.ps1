[CmdletBinding()]
param(
    [string]$VmHost = '192.168.115.130',
    [string]$VmUser = 'roots',
    [string]$RemoteDirectory = '/home/roots/aiops-platform',
    [string]$IdentityFile = '',
    [string]$KnownHostsFile = '',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $IdentityFile) { $IdentityFile = Join-Path $root 'data/vm-access/ubuntu_ed25519' }
if (-not $KnownHostsFile) { $KnownHostsFile = Join-Path $root 'data/vm-access/known_hosts' }

try {
    if ($VmHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$' -or
        $VmUser -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_-]*$' -or
        $RemoteDirectory -notmatch '^/[a-zA-Z0-9_./-]+$') {
        throw 'Invalid SSH host, user or remote directory.'
    }
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    foreach ($file in @($IdentityFile, $KnownHostsFile)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Missing SSH file: $file. Supply -IdentityFile / -KnownHostsFile."
        }
    }
    . (Join-Path $PSScriptRoot 'prepare_ssh_identity.ps1')
    $IdentityFile = Initialize-SshIdentity -Source $IdentityFile -Directory (Join-Path $root 'data/startup/ssh')
    $sshArguments = @('-i', $IdentityFile, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$KnownHostsFile",
        '-o', 'ConnectTimeout=10')
    $destination = "$VmUser@$VmHost"
    Write-Host 'Syncing frontend source and deployment checks to Ubuntu VM...'
    $syncTar = Join-Path $root 'data/startup/frontend-sync.tar.gz'
    New-Item -ItemType Directory -Force -Path (Join-Path $root 'data/startup') | Out-Null
    & tar.exe -czf $syncTar -C $root frontend/src frontend/public frontend/index.html scripts/check_platform.py scripts/start_all.sh
    if ($LASTEXITCODE -ne 0) { throw 'Source archive failed.' }
    $scpArguments = @('-i', $IdentityFile, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$KnownHostsFile",
        $syncTar, "${destination}:/tmp/frontend-sync.tar.gz")
    & scp.exe @scpArguments
    if ($LASTEXITCODE -ne 0) { throw 'Source upload failed.' }
    & $ssh @sshArguments $destination "tar -xzf /tmp/frontend-sync.tar.gz -C $RemoteDirectory && rm -f /tmp/frontend-sync.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed.' }

    Write-Host 'Starting Ubuntu services (existing experiments are preserved)...'
    $script = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'start_all.sh')).Replace("`r`n", "`n")
    ($script.TrimEnd() + "`n# end") | & $ssh @sshArguments $destination "bash -s -- $RemoteDirectory"
    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu startup failed; see FAIL above.' }

    $logDirectory = Join-Path $root 'data/startup'
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $recordPath = Join-Path $logDirectory 'tunnel.json'
    $listeners = @(& netstat.exe -ano -p tcp | ForEach-Object {
        if ($_ -match '^\s*TCP\s+\S+:(5173|8000)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
            [pscustomobject]@{ Port = [int]$matches[1]; OwnerId = [int]$matches[2] }
        }
    })
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect local listening ports.' }
    if ($listeners.Count -gt 0) {
        if (-not (Test-Path -LiteralPath $recordPath)) {
            throw 'Port 5173 or 8000 is occupied. Close the existing forwarding session first.'
        }
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        foreach ($listener in $listeners) {
            $owner = Get-Process -Id $listener.OwnerId -ErrorAction Stop
            if ($owner.ProcessName -ne 'ssh' -or $listener.OwnerId -ne $record.ProcessId -or
                $record.Destination -ne $destination -or $owner.StartTime.ToUniversalTime().ToString('o') -ne $record.StartTime) {
                throw "Local port $($listener.Port) is occupied by another process; it was not stopped."
            }
        }
        Write-Host 'Reusing existing SSH tunnel.'
    } else {
        $tunnelArguments = $sshArguments + @('-N', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-L', '127.0.0.1:5173:127.0.0.1:5173',
            '-L', '127.0.0.1:8000:127.0.0.1:8000', $destination)
        # Start-Process 在 Windows 上需显式保留带空格的参数边界。
        $quotedArguments = $tunnelArguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }
        $tunnel = Start-Process -FilePath $ssh -ArgumentList $quotedArguments -WindowStyle Hidden `
            -RedirectStandardError (Join-Path $logDirectory 'ssh-error.log') `
            -RedirectStandardOutput (Join-Path $logDirectory 'ssh.log') -PassThru
        @{ ProcessId = $tunnel.Id; Destination = $destination; StartTime = $tunnel.StartTime.ToUniversalTime().ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
        Write-Host "SSH tunnel PID: $($tunnel.Id)"
    }
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $api = Invoke-RestMethod 'http://127.0.0.1:8000/api/scenarios' -TimeoutSec 3
            $page = Invoke-WebRequest 'http://127.0.0.1:5173/@vite/client' -UseBasicParsing -TimeoutSec 3
            if ('MYSQL_DOWN' -in $api.key -and $page.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Tunnel readiness failed. Check data/startup/ssh-error.log and Ubuntu data/*.log.' }
    Write-Host 'PASS - Open http://127.0.0.1:5173 (API: http://127.0.0.1:8000/docs)'
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:5173' }
    exit 0
} catch {
    Write-Host "FAIL - $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    exit 1
}
