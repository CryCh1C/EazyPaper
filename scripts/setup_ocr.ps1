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
    try { uv python install 3.12 } catch { Write-Warning "Managed Python download failed; using installed 3.12." }
    uv venv --python 3.12 .venv
}

Write-Host "Installing GUI + OXR runtime into .venv (Python 3.12)..." -ForegroundColor Cyan
uv pip install --python $VenvPython --index-url $IndexUrl -r requirements.txt
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

$ResolvedModel = (Resolve-Path $ModelDir).Path
if (-not (Test-Path (Join-Path $ResolvedModel "model.safetensors"))) {
    throw "OXR weights not found: $ResolvedModel\model.safetensors"
}
Write-Host "`nChecking OXR model..." -ForegroundColor Cyan
& $VenvPython -c "from pathlib import Path; p=Path(r'$ResolvedModel'); print('model:', p); print('config:', (p/'config.json').is_file()); print('weights:', (p/'model.safetensors').is_file())"
if ($LASTEXITCODE -ne 0) { throw "OXR model check failed." }

Write-Host "`nOCR runtime ready." -ForegroundColor Green
Write-Host "  python     : $VenvPython"
Write-Host "  model      : $ResolvedModel"
Write-Host "  backend    : OXR-1.0 (local weights)"
Write-Host "  GPU build  : $Gpu"
Write-Host "  sidecar log: .\logs\ocr-worker.log"
