#Requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:TMP = Join-Path $ProjectRoot ".tmp"
$env:TEMP = $env:TMP
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host "=== EasyPaper doctor ===" -ForegroundColor Cyan
Write-Host "home       : $ProjectRoot"
Write-Host "config     : $(Test-Path config.json)"
Write-Host "exe        : $(Test-Path EasyPaper.exe)"
Write-Host "model      : $((Test-Path 'paddle_ocr\model.safetensors'))"
Write-Host "venv       : $(Test-Path $VenvPython)"
if (Test-Path $VenvPython) {
    & $VenvPython -c "import sys; print('python     :', sys.version.split()[0])"
    & $VenvPython -c "import PySide6, pymupdf, openai; print('pyside6    :', PySide6.__version__); print('pymupdf    :', pymupdf.__version__); print('openai     :', openai.__version__)"
}
if (Test-Path $VenvPython) {
    Write-Host "`n--- OCR runtime ---" -ForegroundColor Yellow
    & $VenvPython tools\ocr_worker.py --check --model-dir paddle_ocr
}
Write-Host "`n--- config (secrets redacted) ---" -ForegroundColor Yellow
if (Test-Path config.json) {
    & $VenvPython -c "import json; c=json.load(open('config.json',encoding='utf-8')); c['llm']['api_key']='***' if c['llm'].get('api_key') else ''; print(json.dumps({'llm':c['llm'],'ocr':{k:c['ocr'][k] for k in ('enabled','mode','model_dir','device')}},ensure_ascii=False,indent=2))"
}
