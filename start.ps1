param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
$agentPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $agentPython)) {
    throw "Create .venv and install requirements first. See README.md."
}
$agentEnvFile = Join-Path $PSScriptRoot ".env"
$agentRuntime = $env:POSTGRES_RUNTIME
if (-not $agentRuntime -and (Test-Path -LiteralPath $agentEnvFile)) {
    $agentRuntimeLine = Get-Content -LiteralPath $agentEnvFile | Where-Object { $_ -match '^POSTGRES_RUNTIME=' } | Select-Object -Last 1
    if ($agentRuntimeLine) { $agentRuntime = ($agentRuntimeLine -split '=',2)[1].Trim() }
}
if ($agentRuntime -eq 'wsl') {
    & wsl -d Ubuntu-22.04 -u root -- pg_isready -q -p 55432
    if ($LASTEXITCODE -ne 0) {
        # A foreground WSL process keeps the distro alive while Windows uses PG.
        Start-Process -FilePath 'wsl.exe' -ArgumentList '-d','Ubuntu-22.04','-u','root','--','pg_ctlcluster','--skip-systemctl-redirect','--foreground','14','power-agent','start' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'data\postgres-runtime.out.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data\postgres-runtime.err.log')
        $agentDatabaseReady = $false
        for ($agentAttempt = 0; $agentAttempt -lt 10; $agentAttempt++) {
            Start-Sleep -Milliseconds 500
            & wsl -d Ubuntu-22.04 -u root -- pg_isready -q -p 55432
            if ($LASTEXITCODE -eq 0) { $agentDatabaseReady = $true; break }
        }
        if (-not $agentDatabaseReady) { throw "PostgreSQL startup failed; see data/postgres-runtime.err.log." }
    }
} elseif ($agentRuntime -eq 'docker') {
    & docker compose --project-directory $PSScriptRoot -f (Join-Path $PSScriptRoot 'compose.yaml') up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container startup failed." }
}
& $agentPython -X utf8 (Join-Path $PSScriptRoot "app.py") --port $Port
exit $LASTEXITCODE
