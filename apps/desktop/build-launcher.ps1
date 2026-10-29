# Install a double-click entry that uses LOCAL Python (no PyInstaller / no embedded python312.dll).
# Creates: desktop .lnk -> pythonw.exe + launcher.py, and apps/desktop/dist/*.cmd fallback.
$ErrorActionPreference = "Stop"
$desktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $desktopDir "..\..")
$launcher = Join-Path $desktopDir "launcher.py"
if (-not (Test-Path $launcher)) { throw "Missing $launcher" }

# Single source of truth for interpreter lookup: find-python.cmd
# (MAS_PYTHON -> repo venv -> py -3 -> common conda dirs -> PATH python).
function Find-PythonW {
  $finder = Join-Path $desktopDir "find-python.cmd"
  if (-not (Test-Path $finder)) { throw "Missing $finder" }
  $out = & cmd.exe /d /c "`"$finder`" --print"
  if ($LASTEXITCODE -ne 0) {
    throw "No Python 3 found (MAS_PYTHON / venv / py -3 / conda / PATH). Set MAS_PYTHON to your python.exe, e.g. D:\ProgramData\miniconda3\python.exe"
  }
  $pyw = $null
  foreach ($line in $out) {
    if ($line -like "MAS_PYW=*") { $pyw = $line.Substring(8).Trim() }
  }
  if (-not $pyw -or -not (Test-Path $pyw)) { throw "find-python.cmd returned no usable pythonw: $out" }
  return (Resolve-Path $pyw).Path
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

# dist\*.cmd just delegate to apps\desktop\start-mas.cmd (same interpreter lookup).
$startCmd = Join-Path $desktopDir "start-mas.cmd"
$cmdBody = "@echo off`r`ncall `"$startCmd`"`r`n"
$cmdAscii = Join-Path $dist "start-mas.cmd"
$cmdBody | Set-Content -Path $cmdAscii -Encoding ASCII
$zhName = ([string][char]0x542F) + ([string][char]0x52A8) + " Multimodal Agent Studio.cmd"
$cmdZh = Join-Path $dist $zhName
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
