# 一键启动（第 88 步 / B3）
#
# 为什么单独写一个：这套东西换机器时最容易出问题的地方不是代码，而是
# "忘了建 venv""忘了装依赖""忘了 cp .env""不知道数据在哪"。
# 这个脚本把这四步串起来，且**每一步都可重复执行**（不会把已有东西覆盖掉）。
#
# 用法：在项目根目录用 PowerShell 跑
#     powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
# 它不做的事：不改 .env 内容、不删任何数据、不装 Python 本体。

param(
    # 第 179 步（评价13 O-2）：默认仍是开发模式（--reload 自动重载）；
    # 加 -Prod 就不带 --reload —— 长期运行/打包后不需要文件监控，
    # 少一份开销，也避免监控句柄一直锁着文件。
    [switch]$Prod
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "项目根目录：$root"

# 1) 虚拟环境
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "[1/4] 创建 .venv ..."
    python -m venv .venv
} else {
    Write-Host "[1/4] .venv 已存在，跳过"
}

# 2) 依赖
Write-Host "[2/4] 安装依赖（requirements.txt 已钉版本）..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

# 3) 配置
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    if (Test-Path (Join-Path $root '.env.example')) {
        Copy-Item (Join-Path $root '.env.example') $envFile
        Write-Host "[3/4] 已从 .env.example 生成 .env —— **记得填 API key**（见 README 的环境变量表）"
    } else {
        Write-Warning "[3/4] 既没有 .env 也没有 .env.example"
    }
} else {
    Write-Host "[3/4] .env 已存在，不覆盖"
}

# 4) 启服务（`-Prod` 时不带 --reload：日常开发才需要文件监控，
#    打包/长期运行时不背这个开销，也避免监控句柄锁住文件）
# `--host 127.0.0.1` 是**刻意写出来的**（评价11 P2-1）：
# 这套 API 没有鉴权，session_id 就是全部凭据。uvicorn 默认也只绑回环，
# 但"默认"不该是安全边界 —— 写死在这里，谁想对外暴露都得先改这一行。
Write-Host "[4/4] 启动：http://127.0.0.1:8000 （Ctrl+C 停止）"
Write-Host "      只监听本机回环，局域网内访问不到（这是有意的，接口没有鉴权）"
Write-Host "      数据在 data\，日志在 data\logs\app.log，备份用 scripts\backup_data.py"
$uvicornArgs = @("-m", "uvicorn", "main:app", "--host", "127.0.0.1")
if (-not $Prod) { $uvicornArgs += "--reload" }
& $venvPython @uvicornArgs
