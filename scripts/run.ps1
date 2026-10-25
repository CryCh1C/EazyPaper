#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Pdf = ""
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:TMP = Join-Path $ProjectRoot ".tmp"
$env:TEMP = $env:TMP
$env:PYTHONPYCACHEPREFIX = Join-Path $ProjectRoot ".pycache"
$env:PYTHONIOENCODING = "utf-8"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "No .venv found. Run .\scripts\build.ps1 or .\scripts\setup_ocr.ps1 first."
}
$argsList = @("-m", "app.main")
if ($Pdf) { $argsList += $Pdf }
& $VenvPython @argsList
