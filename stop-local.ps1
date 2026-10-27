[CmdletBinding(SupportsShouldProcess = $true)]
param([ValidateRange(1, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'

$agentListeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($agentListeners.Count -eq 0) {
    Write-Host '本机服务已经停止。'
    return
}
$agentOwners = @($agentListeners | Select-Object -ExpandProperty OwningProcess -Unique)
if ($agentOwners.Count -ne 1) { throw "端口 $Port 对应多个进程，无法安全关闭，请在原启动窗口按 Ctrl+C。" }

$agentServiceId = [int]$agentOwners[0]
$agentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$agentServiceId"
$agentPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$agentPythonPattern = [regex]::Escape($agentPython)
$agentApp = [regex]::Escape((Join-Path $PSScriptRoot 'app.py'))
# Only accept the launcher's exact app command, never a name-only Python match.
$agentCommandPattern = '^\s*(?:"' + $agentPythonPattern + '"|' + $agentPythonPattern + ')\s+(?:-X\s+utf8\s+)?(?:"' + $agentApp + '"|' + $agentApp + ')(?:\s+--port\s+' + $Port + ')?\s*$'
function Test-LocalProcess($Candidate) {
    if (-not $Candidate -or $Candidate.CommandLine -notmatch $agentCommandPattern) { return $false }
    if ($Candidate.ExecutablePath -eq $agentPython) { return $true }
    # Windows venv launchers delegate to the base interpreter; verify that parent.
    $agentParent = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$Candidate.ParentProcessId)"
    return ($agentParent -and $agentParent.ExecutablePath -eq $agentPython -and
        $agentParent.CommandLine -eq $Candidate.CommandLine -and
        $agentParent.CreationDate -le $Candidate.CreationDate)
}
if (-not (Test-LocalProcess $agentProcess)) {
    throw "端口 $Port 的程序无法确认为本项目启动的服务，未关闭任何进程。请在原启动窗口按 Ctrl+C。"
}

if ($PSCmdlet.ShouldProcess("本项目本机服务（端口 $Port，PID $agentServiceId）", '停止')) {
    # Recheck identity immediately before stopping in case the process exited.
    $agentCurrent = Get-CimInstance Win32_Process -Filter "ProcessId=$agentServiceId"
    if (-not $agentCurrent) { Write-Host '本机服务已经停止。'; return }
    if ($agentCurrent.CreationDate -ne $agentProcess.CreationDate -or
        $agentCurrent.CommandLine -ne $agentProcess.CommandLine -or
        -not (Test-LocalProcess $agentCurrent)) {
        throw '服务进程已变化，本次未执行关闭，请刷新后重试。'
    }
    Stop-Process -Id $agentServiceId -ErrorAction Stop
    Wait-Process -Id $agentServiceId -Timeout 10 -ErrorAction SilentlyContinue
    if (Get-Process -Id $agentServiceId -ErrorAction SilentlyContinue) {
        throw '服务进程尚未退出，请稍后刷新状态。'
    }
    Write-Host '本机服务已关闭。已保存的数据保留；外网分享和数据库继续运行。'
}
