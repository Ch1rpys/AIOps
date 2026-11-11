# 本机演示引导：安装项目内的 uv 与 Python 3.11，再交给 start_demo.py。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$uvDir = Join-Path $root '.tools\uv'
$uv = Join-Path $uvDir 'uv.exe'
New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
if (-not (Test-Path -LiteralPath $uv)) {
    Write-Host '正在把 uv 装进 .tools'
    $env:UV_INSTALL_DIR = $uvDir
    irm https://astral.sh/uv/install.ps1 | iex
}
$env:UV_INSTALL_DIR = $uvDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.tools\python'
& $uv python install 3.11
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uv run --frozen --python 3.11 python scripts/start_demo.py
exit $LASTEXITCODE
