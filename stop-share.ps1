$ErrorActionPreference = 'Stop'
$agentShare = Join-Path $PSScriptRoot 'data\sharing'
$agentStateFile = Join-Path $agentShare 'current.json'
if (-not (Test-Path -LiteralPath $agentStateFile)) { Write-Host 'No share has been started.'; exit }
$agentState = Get-Content -LiteralPath $agentStateFile -Raw | ConvertFrom-Json
if ($agentState.status -eq 'stopped') { Write-Host 'Share is already stopped.'; exit }
if ($agentState.run_id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid share state.' }
New-Item -ItemType File -Path (Join-Path $agentShare ($agentState.run_id + '.stop')) -Force | Out-Null
Write-Host 'Stop requested. The temporary URL will disconnect; your personal workspace remains running.'
