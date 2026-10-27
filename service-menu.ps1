# Save as UTF-8 with BOM for Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Set-Location -LiteralPath $PSScriptRoot

function Test-ServicePort([int]$Port) {
    $agentClient = New-Object System.Net.Sockets.TcpClient
    try {
        $agentConnect = $agentClient.ConnectAsync('127.0.0.1', $Port)
        return ($agentConnect.Wait(250) -and $agentClient.Connected)
    } catch { return $false } finally { $agentClient.Dispose() }
}

function Test-ShareProcess([int]$ProcessId, [string]$Kind) {
    if ($ProcessId -le 0) { return $false }
    $agentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $agentProcess) { return $false }
    if ($Kind -eq 'launcher') {
        $agentExpected = Join-Path $PSScriptRoot 'share.py'
        return ($agentProcess.Name -eq 'python.exe' -and $agentProcess.CommandLine -and
            $agentProcess.CommandLine.IndexOf($agentExpected, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    }
    return ($agentProcess.ExecutablePath -eq (Join-Path $PSScriptRoot 'data\tools\cloudflared.exe'))
}

function Get-ShareStatus {
    $agentResult = [pscustomobject]@{ Text = '未启动'; Url = $null; Ready = $false }
    $agentStatePath = Join-Path $PSScriptRoot 'data\sharing\current.json'
    if (-not (Test-Path -LiteralPath $agentStatePath)) { return $agentResult }
    try {
        $agentState = Get-Content -LiteralPath $agentStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($agentState.status -eq 'stopped') {
            $agentResult.Text = '已停止'
        } elseif (-not (Test-ShareProcess ([int]$agentState.pid) 'launcher')) {
            $agentResult.Text = '未运行（旧地址已隐藏）'
        } elseif ($agentState.run_id -match '^[a-f0-9]{32}$' -and
            (Test-Path -LiteralPath (Join-Path $PSScriptRoot ('data\sharing\' + $agentState.run_id + '.stop')))) {
            $agentResult.Text = '正在停止'
        } elseif ($agentState.status -eq 'ready' -and
            $agentState.url -cmatch '^https://[a-z0-9-]+\.trycloudflare\.com/?$' -and
            (Test-ShareProcess ([int]$agentState.tunnel_pid) 'tunnel') -and (Test-ServicePort 8001)) {
            $agentResult.Text = '本机分享及隧道进程运行中（公网连通性未检测）'
            $agentResult.Url = $agentState.url.TrimEnd('/')
            $agentResult.Ready = $true
        } elseif ($agentState.status -eq 'starting') {
            $agentResult.Text = '启动中，尚未取得公网地址'
        } else {
            $agentResult.Text = '未就绪，请查看分享日志'
        }
    } catch { $agentResult.Text = '状态暂时不可读，请刷新或查看分享日志' }
    return $agentResult
}

function Show-ServiceInfo {
    $agentLocalText = if (Test-ServicePort 8000) { '端口已就绪' } else { '未启动' }
    $agentShareStatus = Get-ShareStatus
    Write-Host "本机服务：$agentLocalText"
    Write-Host '本机个人空间：http://127.0.0.1:8000/' -ForegroundColor Cyan
    Write-Host "外网分享：$($agentShareStatus.Text)"
    if ($agentShareStatus.Ready) {
        Write-Host "公网登录链接：$($agentShareStatus.Url)/login" -ForegroundColor Green
        Write-Host '复制以上链接发给其他用户，或选择 5 用默认浏览器打开。'
    } else {
        Write-Host '公网登录链接：暂无可用地址；启动后选择 7 刷新。'
    }
    Write-Host ''
    Write-Host '登录：本机入口保留原个人空间；公网入口使用各自独立账号密码。'
    Write-Host '管理员账号：admin；初始密码选择 6 查看，修改后使用新密码。'
    Write-Host '分配账号：管理员登录公网入口后，通过“账号管理”创建用户并授权资料库。'
    Write-Host '停止：本机服务选择 8（或启动窗口按 Ctrl+C）；外网分享选择 3。选择 0 只退出菜单。'
    Write-Host '分享时保持电脑联网且不休眠；重新启动后公网链接可能变化。'
    Write-Host ('访问说明：' + (Join-Path $PSScriptRoot 'data\sharing\access.md'))
    Write-Host ('启动日志：' + (Join-Path $PSScriptRoot 'data\sharing\launcher.err.log'))
}

function Invoke-ServiceScript([string]$Name) {
    # Separate process: existing launchers use exit and must not exit this menu.
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $Name)
    if ($LASTEXITCODE -ne 0) {
        throw "操作未正常完成（退出码 $LASTEXITCODE），请查看上方信息。"
    }
}

while ($true) {
    Write-Host ''
    Write-Host '电知 - 服务管理' -ForegroundColor Cyan
    Write-Host '========================================'
    Write-Host '[1] 启动本机服务'
    Write-Host '[2] 启动外网分享（请先启动本机服务）'
    Write-Host '[3] 关闭外网分享'
    Write-Host '[4] 打开本机个人空间'
    Write-Host '[5] 打开公网账号登录页'
    Write-Host '[6] 查看管理员初始账号信息（仅管理员本人）'
    Write-Host '[7] 刷新状态及链接'
    Write-Host '[8] 关闭本机服务'
    Write-Host '[0] 退出此菜单'
    Write-Host '========================================'
    Show-ServiceInfo
    $agentSelection = Read-Host '请选择 [0-8]'
    if ($agentSelection -eq '7') { continue }
    try {
        switch ($agentSelection) {
            '1' {
                if (Test-ServicePort 8000) {
                    Write-Host '端口 8000 已在使用，请选择 4 打开已有页面；无需重复启动。'
                    break
                }
                Write-Host '正在启动数据库和本机服务，请保持此窗口打开。'
                Write-Host '启动后用浏览器打开 http://127.0.0.1:8000/'
                Write-Host '要同时开启外网分享，请再次双击启动服务.bat 并选择 2。'
                Write-Host '在本窗口按 Ctrl+C，或再次双击启动服务.bat 选择 8，可停止本机服务。'
                Invoke-ServiceScript 'start.ps1'
            }
            '2' {
                if ((Get-ShareStatus).Ready) {
                    Write-Host '分享已运行，请选择 5 打开登录页。'
                    Show-ServiceInfo
                    break
                }
                Invoke-ServiceScript 'start-share.ps1'
                Write-Host '正在等待公网地址，最多等待 30 秒……'
                $agentDeadline = [DateTime]::UtcNow.AddSeconds(30)
                do {
                    Start-Sleep -Milliseconds 750
                    $agentShareStatus = Get-ShareStatus
                } while (-not $agentShareStatus.Ready -and [DateTime]::UtcNow -lt $agentDeadline)
                Show-ServiceInfo
                if (-not $agentShareStatus.Ready) {
                    Write-Host '暂未取得可用地址。后台可能仍在启动，请稍后选择 7 刷新；持续失败时检查启动日志。'
                }
            }
            '3' {
                Invoke-ServiceScript 'stop-share.ps1'
                Write-Host '请等待几秒再重新启动分享。关闭本机服务请选择 8。'
            }
            '4' {
                if (-not (Test-ServicePort 8000)) { throw '本机服务尚未启动，请先选择 1。' }
                Start-Process 'http://127.0.0.1:8000/'
            }
            '5' {
                $agentShareStatus = Get-ShareStatus
                if (-not $agentShareStatus.Ready) { throw '外网分享尚未就绪，请先启动或刷新状态。' }
                Start-Process ($agentShareStatus.Url + '/login')
            }
            '6' {
                $agentAdminFile = Join-Path $PSScriptRoot 'data\sharing\admin-access.md'
                if (-not (Test-Path -LiteralPath $agentAdminFile)) { throw '尚未生成管理员初始账号信息，请先启动外网分享。' }
                Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $agentAdminFile + '"')
                Write-Host '管理员初始信息已在记事本打开，请勿将该文件或密码分享给普通用户。'
            }
            '8' {
                Write-Host '正在关闭本机服务，尚未完成的请求会中断。'
                Invoke-ServiceScript 'stop-local.ps1'
                Show-ServiceInfo
            }
            '0' { return }
            default { Write-Host '请输入 0 到 8 之间的菜单编号。' }
        }
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
    [void](Read-Host '按回车返回菜单')
}
