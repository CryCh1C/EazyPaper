#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ModelDir = "oxr",
    [string]$IndexUrl = "https://pypi.mirrors.ustc.edu.cn/simple",
    [switch]$Gpu
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$ResolvedModel = (Resolve-Path -LiteralPath $ModelDir).Path
foreach ($File in @("config.json", "model.safetensors")) {
    if (-not (Test-Path -LiteralPath (Join-Path $ResolvedModel $File) -PathType Leaf)) {
        throw "OXR model file missing: $ResolvedModel\$File"
    }
}
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot ".uv-python"
$env:UV_PYTHON_CACHE_DIR = Join-Path $ProjectRoot ".uv-python-cache"
$env:TMP = Join-Path $ProjectRoot ".tmp"
$env:TEMP = $env:TMP
$env:PYTHONPYCACHEPREFIX = Join-Path $ProjectRoot ".pycache"
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv not found." }
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    uv python install 3.12
    if ($LASTEXITCODE -ne 0) { Write-Warning "Managed Python download failed. Trying an installed Python 3.12." }
    uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python 3.12 OCR environment." }
}

$version = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $version -ne '3.12') { throw "The OCR environment must use Python 3.12." }

Write-Host "Installing the OXR runtime into .venv..." -ForegroundColor Cyan
# Source checkouts also need the GUI dependencies. Installed copies already have them.
if (Test-Path -LiteralPath 'requirements.txt' -PathType Leaf) {
    uv pip install --python $VenvPython --index-url $IndexUrl -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the development app dependencies." }
}
if ($Gpu) {
    Write-Host "Installing CUDA 13.0 torch/torchvision..." -ForegroundColor Yellow
    uv pip install --python $VenvPython `
        --index-url https://download.pytorch.org/whl/cu130 `
        --extra-index-url $IndexUrl `
        --index-strategy unsafe-best-match `
        -r requirements-ocr-gpu.txt
} else {
    uv pip install --python $VenvPython --index-url $IndexUrl -r requirements-ocr.txt
}

if ($LASTEXITCODE -ne 0) { throw "Unable to install the OXR runtime." }

Write-Host "`nOCR runtime ready." -ForegroundColor Green
Write-Host "  python     : $VenvPython"
Write-Host "  model      : $ResolvedModel"
Write-Host "  backend    : OXR-1.0 (local weights)"
Write-Host "  GPU build  : $Gpu"
