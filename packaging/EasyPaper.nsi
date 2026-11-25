Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
CRCCheck on
AllowSkipFiles off

!include "MUI2.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "${PAYLOAD_UNINSTALL}"

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\EasyPaper"

Name "EasyPaper"
OutFile "${SETUP_OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\EasyPaper"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
ShowInstDetails nevershow
ShowUninstDetails nevershow

VIProductVersion "${VERSION_QUAD}"
VIAddVersionKey /LANG=2052 "ProductName" "EasyPaper"
VIAddVersionKey /LANG=2052 "FileDescription" "EasyPaper 安装程序"
VIAddVersionKey /LANG=2052 "CompanyName" "EasyPaper"
VIAddVersionKey /LANG=2052 "LegalCopyright" "EasyPaper contributors"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION_QUAD}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION_DISPLAY}"

!define MUI_ICON "${PROJECT_ROOT}\app\resources\easypaper.ico"
!define MUI_UNICON "${PROJECT_ROOT}\app\resources\easypaper.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\EasyPaper.exe"
!define MUI_FINISHPAGE_RUN_NOTCHECKED

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  ${IfNot} ${AtLeastWin10}
  ${OrIfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "EasyPaper 需要 Windows 10 或更新的 64 位系统。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
FunctionEnd

Section "EasyPaper"
  SetShellVarContext current
  SetOverwrite on
  SetOutPath "$INSTDIR"
  File "${PROJECT_ROOT}\EasyPaper.exe"
  File "${PROJECT_ROOT}\LICENSE"
  File /oname=EasyPaper-README.txt "${PROJECT_ROOT}\release\README.txt"
  File "${PROJECT_ROOT}\requirements-ocr.txt"
  File "${PROJECT_ROOT}\requirements-ocr-gpu.txt"

  SetOutPath "$INSTDIR\scripts"
  File "${PROJECT_ROOT}\scripts\setup_ocr.ps1"
  SetOutPath "$INSTDIR\_internal"
  File /r /x "__pycache__" /x "*.pyc" /x "*.pyo" /x "qtwebengine_devtools_resources.debug.pak" "${PROJECT_ROOT}\_internal\*.*"

  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall-EasyPaper.exe"
  CreateDirectory "$SMPROGRAMS\EasyPaper"
  CreateShortCut "$SMPROGRAMS\EasyPaper\EasyPaper.lnk" "$INSTDIR\EasyPaper.exe"
  CreateShortCut "$DESKTOP\EasyPaper.lnk" "$INSTDIR\EasyPaper.exe"

  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "EasyPaper"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION_DISPLAY}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "EasyPaper"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\EasyPaper.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall-EasyPaper.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall-EasyPaper.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" ${INSTALL_KIB}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$INSTDIR\EasyPaper.exe"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\EasyPaper-README.txt"
  Delete "$INSTDIR\requirements-ocr.txt"
  Delete "$INSTDIR\requirements-ocr-gpu.txt"
  Delete "$INSTDIR\scripts\setup_ocr.ps1"
  RMDir "$INSTDIR\scripts"
  !insertmacro RemoveRuntime
  Delete "$INSTDIR\Uninstall-EasyPaper.exe"
  Delete "$SMPROGRAMS\EasyPaper\EasyPaper.lnk"
  RMDir "$SMPROGRAMS\EasyPaper"
  Delete "$DESKTOP\EasyPaper.lnk"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  RMDir "$INSTDIR"
SectionEnd
