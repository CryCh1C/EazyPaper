#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$CompilerPath,
    [string]$OutputPath = 'release\EasyPaper-Setup.exe'
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $project '_internal'
$appPath = Join-Path $project 'EasyPaper.exe'
$output = [IO.Path]::GetFullPath($OutputPath, $project)
if (!(Test-Path -LiteralPath $appPath -PathType Leaf) -or
    !(Test-Path -LiteralPath (Join-Path $runtime 'PySide6\Qt6WebEngineCore.dll') -PathType Leaf)) {
    throw 'Build the portable app first with scripts\build.ps1.'
}

if (!$CompilerPath) {
    $CompilerPath = (Get-Command makensis.exe -ErrorAction SilentlyContinue).Source
    if (!$CompilerPath -and ${env:ProgramFiles(x86)}) {
        $CompilerPath = Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe'
    }
}
if (!$CompilerPath -or !(Test-Path -LiteralPath $CompilerPath -PathType Leaf)) {
    throw 'Install NSIS or pass the path to makensis.exe with -CompilerPath.'
}
$compiler = (Resolve-Path -LiteralPath $CompilerPath).Path

$items = Get-ChildItem -LiteralPath $runtime -Recurse -Force
if (($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) -or
    ((Get-Item -LiteralPath $runtime).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'The portable runtime contains a filesystem link. Build it again before packaging.'
}
$items = $items | Where-Object {
    $_.FullName -notmatch '[\\/]__pycache__(?:[\\/]|$)' -and
    ($_.PSIsContainer -or ($_.Extension -notin '.pyc', '.pyo' -and
        $_.Name -ne 'qtwebengine_devtools_resources.debug.pak'))
}
$files = @($items | Where-Object { !$_.PSIsContainer })
$runtimeBytes = ($files | Measure-Object -Property Length -Sum).Sum
$app = Get-Item -LiteralPath $appPath
$version = $app.VersionInfo
$versionQuad = '{0}.{1}.{2}.{3}' -f $version.ProductMajorPart, $version.ProductMinorPart,
    $version.ProductBuildPart, $version.ProductPrivatePart
$installKiB = [math]::Ceiling(($runtimeBytes + $app.Length) / 1KB)

# List only shipped files so uninstall leaves user files in place.
$uninstall = @('!macro RemoveRuntime')
$uninstall += foreach ($file in $files) {
    $relative = [IO.Path]::GetRelativePath($project, $file.FullName).Replace('$', '$$')
    '  Delete "$INSTDIR\{0}"' -f $relative
}
$uninstall += foreach ($dir in ($items | Where-Object PSIsContainer | Sort-Object { $_.FullName.Length } -Descending)) {
    $relative = [IO.Path]::GetRelativePath($project, $dir.FullName).Replace('$', '$$')
    '  RMDir "$INSTDIR\{0}"' -f $relative
}
$uninstall += '  RMDir "$INSTDIR\_internal"', '!macroend'

$stage = Join-Path $project (Join-Path '.tmp' ('nsis-' + [Guid]::NewGuid().ToString('N')))
New-Item -Path $stage -ItemType Directory -Force | Out-Null
$manifest = Join-Path $stage 'uninstall.nsh'
$stagedSetup = Join-Path $stage 'EasyPaper-Setup.exe'
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
try {
    $env:TEMP = $stage
    $env:TMP = $stage
    Set-Content -LiteralPath $manifest -Value $uninstall -Encoding utf8BOM
    $script = Join-Path $project 'packaging\EasyPaper.nsi'
    & $compiler /V2 /INPUTCHARSET UTF8 "/DPROJECT_ROOT=$project" "/DSETUP_OUTPUT=$stagedSetup" `
        "/DPAYLOAD_UNINSTALL=$manifest" "/DVERSION_DISPLAY=$($version.ProductVersion)" `
        "/DVERSION_QUAD=$versionQuad" "/DINSTALL_KIB=$installKiB" $script
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $stagedSetup -PathType Leaf)) {
        throw 'NSIS installer build failed.'
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $output) -Force | Out-Null
    Move-Item -LiteralPath $stagedSetup -Destination $output -Force
    Get-Item -LiteralPath $output | Select-Object FullName, Length
} finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    Remove-Item -LiteralPath $manifest, $stagedSetup -ErrorAction SilentlyContinue
    if (!(Get-ChildItem -LiteralPath $stage -Force)) { [IO.Directory]::Delete($stage) }
}
