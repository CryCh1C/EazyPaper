#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Install,
    [string]$IndexUrl = "https://pypi.mirrors.ustc.edu.cn/simple",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:TMP = Join-Path $ProjectRoot ".tmp"
$env:TEMP = $env:TMP
$env:PYTHONPYCACHEPREFIX = Join-Path $ProjectRoot ".pycache"
$env:PYTHONIOENCODING = "utf-8"
$env:QT_QPA_PLATFORM = "offscreen"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { throw "No .venv. Run .\scripts\build.ps1 first." }
if ($Install) {
    uv pip install --python $VenvPython --index-url $IndexUrl -r requirements-dev.txt
}
& $VenvPython -m pytest @PytestArgs
exit $LASTEXITCODE
