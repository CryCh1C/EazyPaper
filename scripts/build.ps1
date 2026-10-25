#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$WithOcr,
    [switch]$Gpu,
    [switch]$Clean,
    [string]$IndexUrl = "https://pypi.mirrors.ustc.edu.cn/simple"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Keep every uv / pip / Python cache inside the project.
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ProjectRoot ".uv-python"
$env:UV_PYTHON_CACHE_DIR = Join-Path $ProjectRoot ".uv-python-cache"
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot ".pip-cache"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ProjectRoot ".pyinstaller-cache"
$env:TMP = Join-Path $ProjectRoot ".tmp"
$env:TEMP = $env:TMP
$env:PYTHONPYCACHEPREFIX = Join-Path $ProjectRoot ".pycache"
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path $env:TMP, ".uv-cache", ".uv-python", ".uv-python-cache", ".pip-cache" | Out-Null

Write-Host "=== EasyPaper build ===" -ForegroundColor Cyan
Write-Host "Project : $ProjectRoot"
Write-Host "Python  : 3.14 (uv virtual environment)"
Write-Host "Index   : $IndexUrl"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found. Install it first: https://docs.astral.sh/uv/"
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "`n[1/4] Creating Python 3.14 venv..." -ForegroundColor Yellow
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv missing" }
    try {
        uv python install 3.14
    } catch {
        Write-Warning "uv python install failed. Falling back to an installed 3.14 interpreter."
    }
    uv venv --python 3.14 .venv
}

if (-not (Test-Path $VenvPython)) {
    throw "Python 3.14 venv was not created at $VenvPython"
}
$Version = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version -ne "3.14") {
    throw "The existing .venv uses Python $Version, expected 3.14. Remove .venv and rerun."
}
Write-Host "`n[2/4] Interpreter: $(& $VenvPython --version)" -ForegroundColor Green

if (-not $SkipInstall) {
    Write-Host "`n[3/4] Installing build dependencies..." -ForegroundColor Yellow
    uv pip install --python $VenvPython --index-url $IndexUrl -r requirements.txt -r requirements-build.txt
    if ($WithOcr) {
        if ($Gpu) {
            Write-Host "Installing GPU OCR sidecar dependencies (CUDA 13.0)..." -ForegroundColor Yellow
            uv pip install --python $VenvPython `
                --index-url https://download.pytorch.org/whl/cu130 `
                --extra-index-url $IndexUrl `
                --index-strategy unsafe-best-match `
                -r requirements-ocr-gpu.txt
        } else {
            Write-Host "Installing OCR sidecar dependencies (CPU torch)..." -ForegroundColor Yellow
            uv pip install --python $VenvPython --index-url $IndexUrl -r requirements-ocr.txt
        }
    }
} else {
    Write-Host "`n[3/4] Skipping dependency installation (-SkipInstall)" -ForegroundColor DarkYellow
}

if ($Clean) {
    Write-Host "`nCleaning previous build artifacts..." -ForegroundColor Yellow
    foreach ($path in @("build", "dist", ".build-dist", "EasyPaper.exe", "_internal", ".easypaper-runtime")) {
        if (Test-Path $path) { Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue }
    }
}

Write-Host "`n[4/4] Running PyInstaller (one-folder)..." -ForegroundColor Yellow
$Spec = Join-Path $ProjectRoot "packaging\EasyPaper.spec"
$Stage = Join-Path $ProjectRoot ".build-dist"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
$env:EASYPAPER_BUILD_ONEFILE = "0"
& $VenvPython -m PyInstaller --noconfirm --clean --distpath $Stage --workpath (Join-Path $ProjectRoot "build") $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$BuiltDir = Join-Path $Stage "EasyPaper"
$BuiltExe = Join-Path $BuiltDir "EasyPaper.exe"
if (-not (Test-Path $BuiltExe)) { throw "Build did not produce $BuiltExe" }

# Place the executable and its private _internal directory at the project root
# so app_home(), config.json, paddle_ocr and cache/ stay side by side.
if (Test-Path (Join-Path $ProjectRoot "EasyPaper.exe")) {
    Remove-Item (Join-Path $ProjectRoot "EasyPaper.exe") -Force
}
if (Test-Path (Join-Path $ProjectRoot "_internal")) {
    Remove-Item (Join-Path $ProjectRoot "_internal") -Recurse -Force
}
Get-ChildItem -LiteralPath $BuiltDir -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $ProjectRoot -Recurse -Force
}
Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue

# Safe size trim: the GUI uses QWidget/QPainter only.  Software OpenGL,
# QtNetwork and SVG are not linked by QtCore/Gui/Widgets or the Windows plugin.
$Internal = Join-Path $ProjectRoot "_internal"
$PruneFiles = @(
    "_internal\PySide6\opengl32sw.dll",
    "_internal\PySide6\Qt6Svg.dll",
    "_internal\PySide6\QtSvg.pyd",
    "_internal\PySide6\plugins\platforms\qdirect2d.dll",
    "_internal\PySide6\plugins\platforms\qminimal.dll",
    "_internal\PySide6\plugins\platforms\qoffscreen.dll"
)
foreach ($relative in $PruneFiles) {
    $target = Join-Path $ProjectRoot $relative
    if (Test-Path $target) { Remove-Item $target -Force -ErrorAction SilentlyContinue }
}
$ImageFormats = Join-Path $Internal "PySide6\plugins\imageformats"
if (Test-Path $ImageFormats) {
    Get-ChildItem $ImageFormats -File | Where-Object { $_.Name -ne "qico.dll" } | Remove-Item -Force -ErrorAction SilentlyContinue
}
foreach ($relative in @("_internal\PySide6\plugins\iconengines", "_internal\PySide6\translations")) {
    $target = Join-Path $ProjectRoot $relative
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
}

$Exe = Join-Path $ProjectRoot "EasyPaper.exe"
$ExeMb = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
$TotalMb = [math]::Round(((Get-Item $Exe).Length + (Get-ChildItem $Internal -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host "`nBUILD OK" -ForegroundColor Green
Write-Host "  $Exe  (exe $ExeMb MB; runtime folder total $TotalMb MB)"
Write-Host "  _internal\  PySide6/PyMuPDF/OpenAI runtime (keep beside the exe)"
Write-Host "  OCR sidecar : .venv\Scripts\python.exe + _internal\tools\ocr_worker.py (not embedded)"
Write-Host "  Model       : .\paddle_ocr"
Write-Host "  Config      : .\config.json"
Write-Host "  Cache/logs  : .\cache, .\logs"
