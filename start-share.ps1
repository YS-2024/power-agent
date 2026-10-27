param([switch]$Resume)
$ErrorActionPreference = 'Stop'
$agentPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$agentShare = Join-Path $PSScriptRoot 'data\sharing'
New-Item -ItemType Directory -Path $agentShare -Force | Out-Null
if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8001 is already in use. Stop the previous share before starting another.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data\tools\cloudflared.exe'))) {
    throw 'Install cloudflared at data/tools/cloudflared.exe. See docs/sharing.md.'
}
$agentStateFile = Join-Path $agentShare 'current.json'
if (Test-Path -LiteralPath $agentStateFile) {
    $agentState = Get-Content -LiteralPath $agentStateFile -Raw | ConvertFrom-Json
    if ($agentState.status -ne 'stopped') {
        $agentPriorLauncher = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$agentState.pid)" -ErrorAction SilentlyContinue
        $agentPriorTunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$agentState.tunnel_pid)" -ErrorAction SilentlyContinue
        if (($agentPriorLauncher -and $agentPriorLauncher.CommandLine -like '*share.py*') -or ($agentPriorTunnel -and $agentPriorTunnel.Name -eq 'cloudflared.exe')) {
            throw 'The previous share is still running. Run stop-share.ps1 and wait before restarting.'
        }
        # After a computer restart no old processes remain; preserve accounts and data.
        $agentState.status = 'stopped'
        [System.IO.File]::WriteAllText($agentStateFile, ($agentState | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    }
}
$agentShareArgs = @('-X','utf8',(Join-Path $PSScriptRoot 'share.py'))
if ($Resume) { $agentShareArgs += '--resume' }
Start-Process -FilePath $agentPython -ArgumentList $agentShareArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $agentShare 'launcher.out.log') -RedirectStandardError (Join-Path $agentShare 'launcher.err.log') | Out-Null
Write-Host 'Starting share. Access details will be saved to data/sharing/access.md. Keep this computer online.'
