# 项目专用 PostgreSQL 本地实例管理(开发用)。
# 数据目录 .pgdata/(已 gitignore),端口 5433,用户 geo,trust 认证(仅本机)。
#
# 用法:
#   .\scripts\pgdev.ps1 init     # 首次初始化(initdb + 建库 geo_tool)
#   .\scripts\pgdev.ps1 start    # 启动
#   .\scripts\pgdev.ps1 stop     # 停止
#   .\scripts\pgdev.ps1 status   # 状态

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("init", "start", "stop", "status")]
    [string]$Action
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $RepoRoot ".pgdata"
$LogFile = Join-Path $DataDir "server.log"
$Port = 5433
$User = "geo"
$Db = "geo_tool"

$PgBin = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $PgBin) { throw "未找到 PostgreSQL 安装(C:\Program Files\PostgreSQL\<ver>\bin)" }

switch ($Action) {
    "init" {
        if (Test-Path (Join-Path $DataDir "PG_VERSION")) {
            Write-Host "已初始化过($DataDir),跳过 initdb"
        } else {
            & (Join-Path $PgBin "initdb.exe") -D $DataDir -U $User -E UTF8 --locale=C -A trust
            if ($LASTEXITCODE -ne 0) { throw "initdb 失败" }
        }
        & (Join-Path $PgBin "pg_ctl.exe") -D $DataDir -o "-p $Port" -l $LogFile -w start
        & (Join-Path $PgBin "psql.exe") -p $Port -U $User -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$Db'" | Out-String -OutVariable exists | Out-Null
        if ($exists.Trim() -ne "1") {
            & (Join-Path $PgBin "createdb.exe") -p $Port -U $User $Db
            if ($LASTEXITCODE -ne 0) { throw "createdb 失败" }
            Write-Host "已创建数据库 $Db"
        }
        Write-Host "就绪:postgresql://$User@localhost:$Port/$Db"
    }
    "start"  { & (Join-Path $PgBin "pg_ctl.exe") -D $DataDir -o "-p $Port" -l $LogFile -w start }
    "stop"   { & (Join-Path $PgBin "pg_ctl.exe") -D $DataDir stop }
    "status" { & (Join-Path $PgBin "pg_ctl.exe") -D $DataDir status }
}
