# 打包成桌面程序（Windows）
# 用法：.\build_windows.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw '请先运行 启动检索.bat 安装项目虚拟环境，再关闭服务后打包。' }
& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败' }
& $python tools\build.py @args
if ($LASTEXITCODE -ne 0) { throw '打包失败' }

$exe = Join-Path $root 'dist\EveryFind\EveryFind.exe'
if (Test-Path $exe) {
    Write-Host ''
    Write-Host '打包完成：' -ForegroundColor Green
    Write-Host "  $exe"
    Write-Host '把这个文件夹整个拷走即可使用，首次运行会在同级目录生成 data\。'
}
