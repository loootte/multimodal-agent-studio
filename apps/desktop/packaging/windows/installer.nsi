# Multimodal Agent Studio — Windows NSIS installer (13.C / #76)
# Build: makensis /DVERSION=0.1.0 /DSTAGE=..\..\..\..\dist\mas-windows installer.nsi
# Unsigned by default (ADR 0010 risk note). Do NOT ship secrets in this script.

!ifndef VERSION
  !define VERSION "0.1.0"
!endif
!ifndef STAGE
  !define STAGE "..\..\..\..\dist\mas-windows"
!endif

Name "Multimodal Agent Studio"
OutFile "..\..\..\..\dist\MultimodalAgentStudio-${VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\MultimodalAgentStudio"
InstallDirRegKey HKCU "Software\MultimodalAgentStudio" "InstallDir"
RequestExecutionLevel user
Unicode true
SetCompressor /SOLID lzma

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage components
UninstPage instfiles

Section "Multimodal Agent Studio (required)" SecApp
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*.*"
  WriteRegStr HKCU "Software\MultimodalAgentStudio" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\MultimodalAgentStudio" "Version" "${VERSION}"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Multimodal Agent Studio"
  CreateShortCut "$SMPROGRAMS\Multimodal Agent Studio\Multimodal Agent Studio.lnk" \
    "$INSTDIR\start-mas.cmd" "" "$INSTDIR\icon.ico" 0
  CreateShortCut "$DESKTOP\Multimodal Agent Studio.lnk" \
    "$INSTDIR\start-mas.cmd" "" "$INSTDIR\icon.ico" 0
SectionEnd

Section /o "un.删除数据目录和钥匙串条目" UnSecData
  ; Optional: only when user ticks this. Default is KEEP data.
  ; Must run BEFORE UnSecApp so runtime\python still exists for keychain purge.
  ; Data dir default: $LOCALAPPDATA\MultimodalAgentStudio\data
  IfFileExists "$INSTDIR\runtime\python\python.exe" 0 +2
    ExecWait '"$INSTDIR\runtime\python\python.exe" "$INSTDIR\app\apps\desktop\keychain_tool.py" purge-all'
  RMDir /r "$LOCALAPPDATA\MultimodalAgentStudio\data"
  Delete "$LOCALAPPDATA\MultimodalAgentStudio\desktop.json"
SectionEnd

Section "un.Multimodal Agent Studio" UnSecApp
  Delete "$DESKTOP\Multimodal Agent Studio.lnk"
  RMDir /r "$SMPROGRAMS\Multimodal Agent Studio"
  Delete "$INSTDIR\Uninstall.exe"
  ; Remove app files but NOT user data / keychain by default
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\start-mas.cmd"
  Delete "$INSTDIR\find-python.cmd"
  Delete "$INSTDIR\icon.ico"
  Delete "$INSTDIR\README.txt"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\MultimodalAgentStudio"
SectionEnd
