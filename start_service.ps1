# ============================================================
#  FLUX 文生图服务 / 小红书发布服务  一键启动（由 start_service.bat 或各"启动-*.bat"调用）
#  -Target flux   : 只起 FLUX 文生图服务 (port 9620) + 确保隧道
#  -Target xhs    : 只起 小红书发布服务 (port 8800) + 确保隧道
#  -Target tunnel : 只起 cloudflared 隧道 (xhs-tunnel, 公网 flux/xhs 域名共用)
#  -Target all    : flux + xhs + 隧道（默认）
#  -Restart       : 先停掉已占端口的进程再起（**改了代码之后必须加**）
#  默认为幂等：已在运行则跳过，关掉窗口不中断服务。
#
#  ⚠️ 为什么有 -Restart（2026-09-21 实测事故，值得读完）：
#    Python 进程在 import 时把代码读进内存，**之后不再读盘**。原脚本对已占端口
#    只打印"已在运行，跳过" —— 没有任何重启路径，于是"改了代码 → 点 .bat"
#    永远跑的还是旧逻辑，而输出**看起来像成功启动了**。
#    症状是前端调新接口报 404，且 /health 的 web_uptime_sec 一路涨
#    （实测两次点击间隔 24 分钟，uptime 恰好 +1442 秒 = 从未重启）。
#    → 改了 manager/ 或 server/ 下的代码后：`.\start_service.ps1 -Target flux -Restart`
#    → 或直接用 各"启动-*-重启.bat"
#    验收：py -3.11 tools/check_stale.py（双判据）
#
#  隧道约定：本地只跑 xhs-tunnel(27da88b4)，FLUX 与小红书域名都走它。
#  严禁用旧 transcribe-bot 隧道(1779cc80) 重启本地 cloudflared（会抢 VPS 隧道）。
#  详见 C:\Users\Dancing\.cloudflared\COORDINATION.md
# ============================================================

param([string]$Target = "all", [switch]$Restart)

$ErrorActionPreference = 'SilentlyContinue'

$script:hadFailure = $false

# ============================================================
#  [0] 环境自愈：消除「大小写重复的代理变量」导致的 PS 5.1 崩溃
#      ⚠️ 为什么必须先做这一步（2026-09-21 实测事故，必读）：
#      本机环境同时存在 `http_proxy` 与 `HTTP_PROXY`（https 同理）。
#      PowerShell 5.1 的环境变量提供程序 **大小写不敏感**，所以
#        `Get-ChildItem env:` → 抛「已添加了具有相同键的项」
#      （实测：连续 6 次全部抛，非偶发；.NET 侧可看到 4 个重复名）
#      Start-Process 带 -RedirectStandardOutput 时会去枚举环境 → 同样抛。
#      ★ 致命之处：抛出点在 **Stop-Process 之后**，于是
#        「-Restart」= 先杀掉服务 → 再崩溃 → **端口 9620 直接死掉**。
#      症状极具误导性：看起来"什么都没发生"，实际服务已被干掉。
#      → 这里把重复键收敛成一个（保留值），让后续一切恢复正常。
# ============================================================
function Repair-DuplicateEnv {
    $names = @('http_proxy', 'https_proxy', 'no_proxy', 'all_proxy')
    $fixed = @()
    foreach ($lower in $names) {
        $upper = $lower.ToUpper()
        $vLow  = [System.Environment]::GetEnvironmentVariable($lower)
        $vUp   = [System.Environment]::GetEnvironmentVariable($upper)
        if ($null -ne $vLow -and $null -ne $vUp) {
            # 两个都在 → 删掉「大写」那个（保留小写，惯例更常见）
            [System.Environment]::SetEnvironmentVariable($upper, $null)
            $fixed += "$upper"
        }
    }
    return $fixed
}
$fixedEnv = Repair-DuplicateEnv
if ($fixedEnv.Count -gt 0) {
    Write-Host "[env] 已消除重复的大写代理变量: $($fixedEnv -join ', ')"
}

$ProjFlux   = 'E:\myClaudCodeWorkspace\flux-t2i-server'
$ProjXhs    = 'E:\myClaudCodeWorkspace\xhs-note-publish'
$Python     = 'C:\Users\Dancing\AppData\Local\Programs\Python\Python311\python.exe'
$Cloudflared = 'C:\Users\Dancing\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe'
$CfDir      = 'C:\Users\Dancing\.cloudflared'

function Test-Port($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# 杀掉占用某端口的进程（用于 -Restart）。
# ⚠️ 为什么必须有这个函数（2026-09-21 实测事故）：
#   原逻辑只做 `if (Test-Port 9620) { 跳过 }` —— **没有任何重启路径**。
#   Python 进程 import 时把代码读进内存、之后不再读盘，于是「改了代码 → 点 .bat」
#   永远还是旧逻辑。症状是前端新接口 404，而 /health 的 web_uptime_sec 一路涨
#   （实测两次点击间隔 24 分钟，uptime 正好 +1442 秒 = 从未重启）。
#   更阴的是脚本会打印"已在运行，跳过"—— **看起来像成功启动了**。
function Stop-PortOwner($port) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        try {
            $p = Get-Process -Id $procId -ErrorAction Stop
            Write-Host "      - 停止占用 $port 的进程 PID=$procId ($($p.ProcessName))"
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-Host "      - ⚠️ 停止 PID=$procId 失败：$($_.Exception.Message)"
        }
    }
    # 等端口真正释放（TIME_WAIT / 子进程回收），最多 10s
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-Port $port)) { return }
    }
    Write-Host "      - ⚠️ $port 等待 10s 仍未释放，继续尝试启动"
}
function Ensure-Tunnel {
    if (Get-Process cloudflared -ErrorAction SilentlyContinue) {
        Write-Host "  隧道 xhs-tunnel 已在运行，跳过"
        return
    }
    Write-Host "  启动 cloudflared 隧道 xhs-tunnel (公网 flux/xhs 域名)..."
    # 正确做法：cd 到 .cloudflared 目录让 cloudflared 自动加载 config.yml；不加 --config 避免参数拆分 bug
    Start-Process -FilePath $Cloudflared -ArgumentList 'tunnel run xhs-tunnel' `
        -WorkingDirectory $CfDir -WindowStyle Hidden `
        -RedirectStandardError  "$CfDir\cf_err.log" `
        -RedirectStandardOutput "$CfDir\cf_out.log"
}

# 启动一个后台服务，并**验证它真的起来了**。
# ⚠️ 为什么不能只写一行 Start-Process（2026-09-21 实测事故）：
#   环境里 `Get-ChildItem env:` 会抛「已添加了具有相同键的项」（大小写重复的 proxy 变量）。
#   Start-Process 带 -RedirectStandardOutput 时会枚举环境 → 同样抛。
#   而它的抛出点在 Stop-Process **之后**，于是 -Restart 会「先杀掉、再崩溃」，
#   端口直接死掉，输出看起来却像什么都没发生。
#   → 这里：① 先自愈环境（见 Repair-DuplicateEnv）
#            ② 起不来就退回「不支持时用 -Verb 或直接调用 python」的兜底
#            ③ **验证端口真的在监听**，否则报红并给出日志位置
function Start-BackgroundService($name, $exe, $args_, $workDir, $outLog, $errLog, $port) {
    $started = $false
    try {
        Start-Process -FilePath $exe -ArgumentList $args_ `
            -WorkingDirectory $workDir -WindowStyle Hidden `
            -RedirectStandardOutput $outLog `
            -RedirectStandardError  $errLog -ErrorAction Stop
        $started = $true
    } catch {
        Write-Host "      - ⚠️ Start-Process 失败：$($_.Exception.Message)"
        Write-Host "      - 回退：尝试不带重定向启动"
        try {
            Start-Process -FilePath $exe -ArgumentList $args_ `
                -WorkingDirectory $workDir -WindowStyle Hidden -ErrorAction Stop
            $started = $true
        } catch {
            Write-Host "      - ❌ 回退仍失败：$($_.Exception.Message)"
        }
    }
    if (-not $started) { return $false }

    # 等待端口真正监听（最多 ~12s）—— 起了但不监听等于没起
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port $port) { return $true }
    }
    Write-Host "      - ❌ $name 已启动但 $port 未在监听，请查日志：$errLog"
    return $false
}

Write-Host ""
Write-Host "============================================"
Write-Host "  服务启动 ($Target)"
Write-Host "============================================"

if ($Target -eq "flux" -or $Target -eq "all") {
    Write-Host " [FLUX] 文生图服务 (port 9620)..."
    # ⚠️ 端口被占 ≠ 代码是新的。Python 进程不会自动重载代码，所以：
    #   - 默认：已在运行就跳过（不动正在服务的进程，避免误杀）
    #   - -Restart：先停掉再起（**改了代码之后必须加这个**）
    if (Test-Port 9620) {
        if ($Restart) {
            Write-Host "      - -Restart：重启以加载最新代码"
            Stop-PortOwner 9620
        } else {
            Write-Host "      - flux_service 已在运行，跳过"
            Write-Host "        ⚠️ 若你刚改过代码，这里**不会**加载新代码！"
            Write-Host "           请改用： .\start_service.ps1 -Target flux -Restart"
        }
    }
    if (-not (Test-Port 9620)) {
        if (Start-BackgroundService 'flux_service' $Python 'manager/flux_service.py' `
                $ProjFlux "$ProjFlux\manager\flux_service.out.log" `
                "$ProjFlux\manager\flux_service.err.log" 9620) {
            Write-Host "      - ✅ 已后台启动并监听 9620 (日志: manager\flux_service.err.log)"
        } else {
            $script:hadFailure = $true
        }
    }
    Ensure-Tunnel
}

if ($Target -eq "xhs" -or $Target -eq "all") {
    Write-Host " [XHS] 小红书发布服务 (port 8800)..."
    if (Test-Port 8800) {
        if ($Restart) {
            Write-Host "      - -Restart：重启以加载最新代码"
            Stop-PortOwner 8800
        } else {
            Write-Host "      - xhs 发布服务已在运行，跳过"
            Write-Host "        ⚠️ 刚改过代码的话请加 -Restart"
        }
    }
    if (-not (Test-Port 8800)) {
        if (Start-BackgroundService 'xhs' $Python 'app.py' `
                $ProjXhs "$ProjXhs\server.out.log" `
                "$ProjXhs\server.err.log" 8800) {
            Write-Host "      - ✅ 已后台启动并监听 8800 (日志: server.err.log)"
        } else {
            $script:hadFailure = $true
        }
    }
    Ensure-Tunnel
}

if ($Target -eq "tunnel") {
    Ensure-Tunnel
}

Write-Host ""
Write-Host "============================================"
if ($script:hadFailure) {
    Write-Host "  ⚠️ 部分服务未能确认启动（见上面 ❌）"
    Write-Host "     排查：查对应 *.err.log；确认解释器路径存在；端口是否被别的程序占用"
} else {
    Write-Host "  OK 完成!  检查:"
    Write-Host "     FLUX:   http://localhost:9620  | https://flux.zhuanlu.xyz"
    Write-Host "     XHS:    http://localhost:8800  | https://xhs.zhuanlu.xyz"
}
Write-Host "============================================"
Start-Sleep -Seconds 2
if ($script:hadFailure) { exit 1 } else { exit 0 }