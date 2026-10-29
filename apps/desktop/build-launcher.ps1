# Install a double-click entry that uses LOCAL Python (no PyInstaller / no embedded python312.dll).
# Creates: desktop .lnk -> pythonw.exe + launcher.py, and apps/desktop/dist/*.cmd fallback.
$ErrorActionPreference = "Stop"
$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $desktopDir "..\..")
$launcher = Join-Path $desktopDir "launcher.py"
if (-not (Test-Path $launcher)) { throw "Missing $launcher" }

function Find-PythonW {
  if ($env:MAS_PYTHON) {
    $p = $env:MAS_PYTHON
    if ($p -match '(?i)python\.exe$') {
      $pw = $p -replace '(?i)python\.exe$', 'pythonw.exe'
      if (Test-Path $pw) { return (Resolve-Path $pw).Path }
    }
    if (Test-Path $p) { return (Resolve-Path $p).Path }
  }
  foreach ($name in @("pythonw.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
  }
  throw "No pythonw/python on PATH. Install Python 3 or set MAS_PYTHON."
}

$pyw = Find-PythonW
Write-Host "Using interpreter: $pyw"

$dist = Join-Path $desktopDir "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# Retire broken PyInstaller onefile exe (python312.dll LoadLibrary failures).
$badExe = Join-Path $dist "MultimodalAgentStudio.exe"
$badMark = Join-Path $dist "DO_NOT_USE_onefile_exe.txt"
if (Test-Path $badExe) {
  $bak = Join-Path $dist "MultimodalAgentStudio.exe.BROKEN-onefile.bak"
  if (Test-Path $bak) { Remove-Item -Force $bak }
  Move-Item -Force $badExe $bak
  Write-Host "Retired broken onefile exe -> $bak"
}
@"
The previous MultimodalAgentStudio.exe was a PyInstaller --onefile bundle.
It unpacks python312.dll under %TEMP%\_MEI* and often fails with:
  Failed to load Python DLL ... LoadLibrary: not enough memory
Do NOT restore or rebuild that onefile exe.
Use the desktop shortcut or the .cmd in this folder (local Python only).
Tauri release exe (when MSVC is available) may later replace the name MultimodalAgentStudio.exe via copy-dist.ps1.
"@ | Set-Content -Path $badMark -Encoding UTF8

# ASCII-named cmd (reliable) + Chinese-named copy for double-click UX.
$cmdBody = @"
@echo off
setlocal
cd /d "$root"
set "MAS_REPO_ROOT=$root"
if defined MAS_PYTHON (
  set "PY=%MAS_PYTHON%"
) else (
  where pythonw >nul 2>&1 && set "PY=pythonw" || set "PY=python"
)
"%PY%" "$launcher"
if errorlevel 1 (
  echo.
  echo Launch failed. Need local Python 3 and full repo checkout.
  echo Interpreter: %PY%
  pause
)
"@
$cmdAscii = Join-Path $dist "start-mas.cmd"
$cmdBody | Set-Content -Path $cmdAscii -Encoding ASCII
$cmdZh = Join-Path $dist "启动 Multimodal Agent Studio.cmd"
# Copy via .NET to keep Unicode filename on Windows PowerShell 5.1
[System.IO.File]::WriteAllText($cmdZh, $cmdBody, [System.Text.Encoding]::ASCII)
Write-Host "cmd: $cmdAscii"
Write-Host "cmd: $cmdZh"

# Desktop shortcut -> pythonw + launcher.py (no embedded DLL).
$desk = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desk "Multimodal Agent Studio.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pyw
$lnk.Arguments = "`"$launcher`""
$lnk.WorkingDirectory = "$root"
$lnk.WindowStyle = 1
$lnk.Description = "Multimodal Agent Studio (local Python sidecar)"
$ico = Join-Path $desktopDir "src-tauri\icons\icon.ico"
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Save()
Write-Host "shortcut: $lnkPath"
Write-Host "Target: $pyw"
Write-Host "Args: `"$launcher`""
Write-Host "Done. Double-click the desktop shortcut (uses local Python, not a bundled DLL)."
