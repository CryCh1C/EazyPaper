#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ModelDir = "paddle_ocr",
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
    try { uv python install 3.14 } catch { Write-Warning "Managed Python download failed; using installed 3.14." }
    uv venv --python 3.14 .venv
}

Write-Host "Installing GUI + OCR runtime into .venv (Python 3.14)..." -ForegroundColor Cyan
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
    throw "PaddleOCR-VL weights not found: $ResolvedModel\model.safetensors"
}
Write-Host "`nChecking OCR worker..." -ForegroundColor Cyan
& $VenvPython tools\ocr_worker.py --check --model-dir $ResolvedModel
if ($LASTEXITCODE -ne 0) { throw "OCR runtime check failed." }

Write-Host "`nOCR runtime ready." -ForegroundColor Green
Write-Host "  python     : $VenvPython"
Write-Host "  model      : $ResolvedModel"
Write-Host "  backend    : transformers PaddleOCR-VL (local weights)"
Write-Host "  GPU build  : $Gpu"
Write-Host "  sidecar log: .\logs\ocr-worker.log"
