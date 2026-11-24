# 项目专用 PostgreSQL 本地实例管理(开发用)。
# 数据目录 .pgdata/(已 gitignore),端口 5433,用户 geo,trust 认证(仅本机)。
#
# 用法:
#   .\scripts\pgdev.ps1 init      # 首次初始化(initdb + 建库 geo_toast)
#   .\scripts\pgdev.ps1 start     # 启动并等到真的能连(最多 180 秒);已在跑则直接返回
#   .\scripts\pgdev.ps1 status    # 三态:RUNNING_OK / PG_ZOMBIE(在听但不应答)/ STOPPED
#   .\scripts\pgdev.ps1 stop      # 正常停止(30 秒内停不掉会提示 kill)
#   .\scripts\pgdev.ps1 kill      # 强杀主进程 + 清锁文件(僵尸专用;已提交的数据由 WAL 保证不丢)
#   .\scripts\pgdev.ps1 restart   # kill(如有必要)+ start
#
# 退出码 / 错误代码(2026-09-25 事故后加,见 docs/错误日志.md):
#   0  就绪 / 已停止(按动作语义)
#   3  STOPPED:没在跑(status 用)
#   10 PG_START_TIMEOUT:启动后 180 秒仍连不上,看 .pgdata/server*.log
#   11 PG_ZOMBIE:端口在听但不接连接(典型:杀毒软件杀了子进程后 error code 487),跑 restart
#   12 PG_STOP_TIMEOUT:正常停止 30 秒停不掉,跑 kill
#   13 PG_START_FAILED:pg_ctl start 本身失败(常见:日志文件被杀毒占着,脚本会自动换日志名再试一次;
#      日志里是「could not bind … Permission denied」时改为不经 pg_ctl、直接启动 postgres.exe,见下面 Start-Direct)

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("init", "start", "stop", "status", "kill", "restart")]
    [string]$Action
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $RepoRoot ".pgdata"
$PidFile = Join-Path $DataDir "postmaster.pid"
$Port = 5433
$User = "geo"
$Db = "geo_toast"  # v0.4.26 S2 起的新库(表结构换代);旧方法的数据留在 geo_tool,原样不动
$StartTimeoutSec = 180

$PgBin = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $PgBin) { throw "未找到 PostgreSQL 安装(C:\Program Files\PostgreSQL\<ver>\bin)" }
$PgCtl = Join-Path $PgBin "pg_ctl.exe"
$PgIsReady = Join-Path $PgBin "pg_isready.exe"

function Test-Listening {
    $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Test-Answering {
    # 端口在听不等于能用:2026-09-25 主进程活着却起不了子进程。真探活只认 pg_isready。
    # 指定连 postgres 库:不指定就按用户名找 geo 库,每探一次服务器日志里多一行「database "geo" does not exist」
    & $PgIsReady -h 127.0.0.1 -p $Port -U $User -d postgres -t 3 *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-PostmasterPid {
    if (Test-Path $PidFile) {
        $first = (Get-Content $PidFile -TotalCount 1).Trim()
        if ($first -match '^\d+$') { return [int]$first }
    }
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Start-PgCtl([string]$log) {
    # 必须用隐藏窗口的 Start-Process,不能用 & 直接调用:postgres 会继承调用方的输出句柄,
    # 在输出被重定向的环境里(Claude 的命令工具、计划任务)调用方要等数据库停掉才读到"输出结束",
    # 脚本永远卡在这一行(2026-09-29 实测,见 docs/错误日志.md)。隐藏窗口启动不继承句柄。
    $argLine = "-D `"$DataDir`" -o `"-p $Port`" -l `"$log`" -w -t $StartTimeoutSec start"
    $proc = Start-Process -FilePath $PgCtl -ArgumentList $argLine -WindowStyle Hidden -PassThru
    $null = $proc.Handle  # 先取句柄,之后才读得到 ExitCode(Windows PowerShell 的已知怪癖)
    return $proc
}

function Start-Direct([string]$log) {
    # 退路:pg_ctl 拉起的 postgres 报「could not bind … Permission denied」时,不经 pg_ctl、用当前身份直接起 postgres.exe。
    # 原因没查明(10-27、10-29 两次;不是沙箱、不是端口被占或被预留,同一时刻 Python 能绑同一端口)。10-29 观察到:pg_ctl 连败三次后,
    # 第一次直接起就绑上了,之后 pg_ctl 怎么起都行——直接起是否真起了作用、还是恰好赶上它自己好了,分不清(见 docs/错误日志.md)。
    # 管理员身份下 postgres.exe 会拒绝运行——那种机器上 pg_ctl 本身能起,走不到这里。
    # 同样用隐藏窗口的 Start-Process,输出只进日志文件,调用方不等它(理由同 Start-PgCtl)。
    $postgres = Join-Path $PgBin "postgres.exe"
    $argLine = "-D `"$DataDir`" -p $Port"
    $proc = Start-Process -FilePath $postgres -ArgumentList $argLine -WindowStyle Hidden -RedirectStandardError $log -PassThru
    $null = $proc.Handle
    return $proc
}

function Test-BindDenied([string]$log) {
    # 只看日志末尾:server.log 是累积的,早先几次失败的旧行不该算进这一次
    $tail = Get-Content $log -Tail 20 -ErrorAction SilentlyContinue | Out-String
    return ($tail -match "could not bind" -and $tail -match "Permission denied")
}

function Wait-Ready($proc, [datetime]$deadline) {
    # 返回 ready(能连)/ failed(pg_ctl 报启动失败)/ timeout;每 5 秒打一个点,等待有上限、有回报。
    while ((Get-Date) -lt $deadline) {
        if (Test-Answering) { return "ready" }
        if ($proc.HasExited -and $proc.ExitCode -ne 0) { return "failed" }
        Write-Host -NoNewline "."
        Start-Sleep -Seconds 5
    }
    return "timeout"
}

function Start-Server {
    if (Test-Answering) { Write-Host "已在跑,能连:postgresql://$User@localhost:$Port/$Db"; return 0 }
    if (Test-Listening) {
        Write-Host "[PG_ZOMBIE] 端口 $Port 在听但不接连接(主进程活着、子进程起不来,常见于杀毒软件干扰)。"
        Write-Host "  下一步:.\scripts\pgdev.ps1 restart"
        return 11
    }
    Write-Host -NoNewline "启动中,等待数据库应答(最多 $StartTimeoutSec 秒)"
    $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
    $log = Join-Path $DataDir "server.log"
    $state = Wait-Ready (Start-PgCtl $log) $deadline
    if ($state -eq "failed" -and -not (Test-Listening) -and -not (Test-BindDenied $log)) {
        $log = Join-Path $DataDir ("server-{0:yyyyMMdd-HHmm}.log" -f (Get-Date))
        Write-Host ""
        Write-Host -NoNewline "pg_ctl start 失败(多半是 server.log 被杀毒软件占着),换日志文件再试一次:$log"
        $state = Wait-Ready (Start-PgCtl $log) $deadline
    }
    if ($state -eq "failed" -and -not (Test-Listening) -and (Test-BindDenied $log)) {
        $log = Join-Path $DataDir ("server-{0:yyyyMMdd-HHmm}-direct.log" -f (Get-Date))
        Write-Host ""
        Write-Host -NoNewline "pg_ctl 拉起的数据库绑不上端口(降权身份被拒),改为直接启动 postgres.exe:$log"
        $state = Wait-Ready (Start-Direct $log) $deadline
    }
    Write-Host ""
    if ($state -eq "ready") { Write-Host "就绪:postgresql://$User@localhost:$Port/$Db"; return 0 }
    if ($state -eq "failed") {
        Write-Host "[PG_START_FAILED] pg_ctl start 报告启动失败。看 $log,或先 .\scripts\pgdev.ps1 kill 再试。"
        return 13
    }
    Write-Host "[PG_START_TIMEOUT] $StartTimeoutSec 秒后仍连不上。看 $log;若端口在听,跑 .\scripts\pgdev.ps1 restart。"
    return 10
}

function Kill-Server {
    $pgPid = Get-PostmasterPid
    if ($pgPid) {
        Stop-Process -Id $pgPid -Force -ErrorAction SilentlyContinue
        Write-Host "已强杀主进程 $pgPid(已提交的数据由 WAL 保证不丢,下次启动自动恢复)"
        Start-Sleep -Seconds 3
    } else {
        Write-Host "没找到主进程"
    }
    Get-Process postgres -ErrorAction SilentlyContinue | Where-Object {
        try { $_.CommandLine -like "*$DataDir*" } catch { $false }
    } | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force; Write-Host "已清除 postmaster.pid" }
    if (Test-Listening) { Write-Host "警告:端口 $Port 仍在监听,再等几秒或手动检查"; return 1 }
    return 0
}

function Ensure-Database {
    # 库不存在就建(换库名后老机器也能直接 start)
    & (Join-Path $PgBin "psql.exe") -p $Port -U $User -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$Db'" | Out-String -OutVariable exists | Out-Null
    if ($exists.Trim() -ne "1") {
        & (Join-Path $PgBin "createdb.exe") -p $Port -U $User $Db
        if ($LASTEXITCODE -ne 0) { throw "createdb 失败" }
        Write-Host "已创建数据库 $Db"
    }
}

$code = 0
switch ($Action) {
    "init" {
        if (Test-Path (Join-Path $DataDir "PG_VERSION")) {
            Write-Host "已初始化过($DataDir),跳过 initdb"
        } else {
            & (Join-Path $PgBin "initdb.exe") -D $DataDir -U $User -E UTF8 --locale=C -A trust
            if ($LASTEXITCODE -ne 0) { throw "initdb 失败" }
        }
        $code = Start-Server
        if ($code -ne 0) { exit $code }
        Ensure-Database
    }
    "start"  { $code = Start-Server; if ($code -eq 0) { Ensure-Database } }
    "status" {
        if (Test-Answering) { Write-Host "RUNNING_OK:在跑且能连(端口 $Port)"; $code = 0 }
        elseif (Test-Listening) { Write-Host "[PG_ZOMBIE] 端口在听但不接连接 → .\scripts\pgdev.ps1 restart"; $code = 11 }
        else { Write-Host "STOPPED:没在跑 → .\scripts\pgdev.ps1 start"; $code = 3 }
    }
    "stop" {
        if (-not (Test-Listening)) { Write-Host "本来就没在跑"; $code = 0 }
        else {
            & $PgCtl -D $DataDir stop -m fast -t 30
            if (Test-Listening) { Write-Host "[PG_STOP_TIMEOUT] 30 秒停不掉 → .\scripts\pgdev.ps1 kill"; $code = 12 }
            else { Write-Host "已停止"; $code = 0 }
        }
    }
    "kill"    { $code = Kill-Server }
    "restart" {
        if (Test-Listening) { $null = Kill-Server }
        $code = Start-Server
    }
}
exit $code
