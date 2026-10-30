# Build a Windows install layout with an embedded/portable Python runtime (13.C / #76).
# Prefer this over PyInstaller onefile (known python312.dll LoadLibrary failures).
#
# Usage (from repo root, PowerShell):
#   powershell -ExecutionPolicy Bypass -File apps\desktop\packaging\windows\build.ps1
#   powershell -File apps\desktop\packaging\windows\build.ps1 -PythonEmbedZip D:\downloads\python-3.12.x-embed-amd64.zip
#
# Outputs:
#   dist\mas-windows\          portable layout (start-mas.cmd works without system Python)
#   dist\MultimodalAgentStudio-<ver>-Setup.exe   if makensis is on PATH
#
# Gap: this environment may lack makensis / a downloaded embed zip. The script still
# produces a smoke layout that reuses -SourcePython (default: MAS_PYTHON / find-python).

param(
  [string]$PythonEmbedZip = "",
  [string]$SourcePython = $env:MAS_PYTHON,
  [string]$OutDir = "",
  [switch]$SkipNsis
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Resolve-Path (Join-Path $Here "..\..")
$Root = Resolve-Path (Join-Path $DesktopDir "..\..")
$Ver = "0.1.0"
try {
  $pkg = Get-Content (Join-Path $DesktopDir "package.json") -Raw | ConvertFrom-Json
  if ($pkg.version) { $Ver = $pkg.version }
} catch {}

if (-not $OutDir) { $OutDir = Join-Path $Root "dist\mas-windows" }
$Stage = $OutDir
Write-Host "Staging -> $Stage"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "runtime\python") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "app") | Out-Null

function Resolve-SourcePython {
  param([string]$Hint)
  if ($Hint -and (Test-Path $Hint)) { return (Resolve-Path $Hint).Path }
  $finder = Join-Path $DesktopDir "find-python.cmd"
  $out = & cmd.exe /d /c "`"$finder`" --print"
  foreach ($line in $out) {
    if ($line -like "MAS_PY=*") { return $line.Substring(7).Trim() }
  }
  throw "No SourcePython. Pass -SourcePython or set MAS_PYTHON."
}

$srcPy = Resolve-SourcePython -Hint $SourcePython
Write-Host "Source Python: $srcPy"

# --- Runtime ---
$runtimePy = Join-Path $Stage "runtime\python"
if ($PythonEmbedZip -and (Test-Path $PythonEmbedZip)) {
  Write-Host "Expanding embeddable Python from $PythonEmbedZip"
  Expand-Archive -Path $PythonEmbedZip -DestinationPath $runtimePy -Force
  # Enable site-packages for embeddables: uncomment import site in ._pth
  Get-ChildItem $runtimePy -Filter "python*._pth" | ForEach-Object {
    $txt = Get-Content $_.FullName
    $txt = $txt | ForEach-Object { if ($_ -match '^#\s*import site') { 'import site' } else { $_ } }
    if ($txt -notcontains 'Lib\site-packages') { $txt += 'Lib\site-packages' }
    Set-Content $_.FullName $txt -Encoding ASCII
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $runtimePy "Lib\site-packages") | Out-Null
  & (Join-Path $runtimePy "python.exe") -m ensurepip --upgrade
  & (Join-Path $runtimePy "python.exe") -m pip install --upgrade pip
} else {
  Write-Host "No embed zip; copying portable venv-style runtime from $srcPy (smoke path)."
  $srcDir = Split-Path -Parent $srcPy
  # Minimal smoke: write a runtime\python\python.cmd that delegates to SourcePython,
  # plus a marker so find-python / start-mas prefer it. Avoids redistributing Miniconda.
  @"
@echo off
REM Smoke runtime shim — replace by building with -PythonEmbedZip for real installs.
set "MAS_SMOKE_RUNTIME=1"
"$srcPy" %*
"@ | Set-Content -Path (Join-Path $runtimePy "python.exe.cmd") -Encoding ASCII
  # Also drop a real python.bat for start-mas; and a pointer file
  @"
@echo off
"$srcPy" %*
"@ | Set-Content -Path (Join-Path $runtimePy "python.bat") -Encoding ASCII
  @"
@echo off
REM pythonw shim
set "_PY=$srcPy"
set "_PYW=%~dp0"
if exist "%~dp0..\..\..\.." (
  rem keep
)
"$srcPy" %*
"@ | Set-Content -Path (Join-Path $runtimePy "pythonw.bat") -Encoding ASCII
  Set-Content -Path (Join-Path $runtimePy "SMOKE_RUNTIME.txt") -Value "Delegates to $srcPy. Rebuild with -PythonEmbedZip for a self-contained install." -Encoding UTF8
  # Copy python.exe/pythonw.exe if they live next to each other AND are embeddable-sized? Skip — use bat.
  # Provide python.exe as a tiny launcher via PowerShell alias file for NSIS shortcuts:
  Copy-Item $srcPy (Join-Path $runtimePy "python.exe") -ErrorAction SilentlyContinue
  $pyw = Join-Path $srcDir "pythonw.exe"
  if (Test-Path $pyw) { Copy-Item $pyw (Join-Path $runtimePy "pythonw.exe") -ErrorAction SilentlyContinue }
}

# Install runtime deps into embedded / smoke interpreter
$pipPy = Join-Path $runtimePy "python.exe"
if (-not (Test-Path $pipPy)) { $pipPy = $srcPy }
Write-Host "Installing app deps with $pipPy"
& $pipPy -m pip install --disable-pip-version-check cryptography pywebview pystray Pillow 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Warning "pip install had errors; continuing (smoke layout may still run if SourcePython already has deps)."
}

# --- App tree (subset) ---
$App = Join-Path $Stage "app"
$copyDirs = @(
  "apps\desktop", "apps\web", "packages", "services", "toolpacks\builtin",
  "docs", "evals", "poc\comfyui"
)
foreach ($rel in $copyDirs) {
  $from = Join-Path $Root $rel
  $to = Join-Path $App $rel
  if (Test-Path $from) {
    New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
    Copy-Item -Recurse -Force $from $to
  }
}
foreach ($f in @("README.md", "ROADMAP.md")) {
  $from = Join-Path $Root $f
  if (Test-Path $from) { Copy-Item $from (Join-Path $App $f) -Force }
}
# Drop caches / local installs / secrets from staged tree
Get-ChildItem $App -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $App -Recurse -Directory -Filter "toolpacks\installed" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $App "apps\desktop\src-tauri") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $App "apps\desktop\dist") -ErrorAction SilentlyContinue

# Entrypoints at layout root
$start = @"
@echo off
setlocal
set "MAS_REPO_ROOT=%~dp0app"
set "MAS_PYTHON=%~dp0runtime\python\python.exe"
if not exist "%MAS_PYTHON%" set "MAS_PYTHON=%~dp0runtime\python\python.bat"
cd /d "%MAS_REPO_ROOT%"
call "%~dp0app\apps\desktop\find-python.cmd"
rem Prefer embedded runtime over system
if exist "%~dp0runtime\python\python.exe" set "MAS_PY=%~dp0runtime\python\python.exe"
if exist "%~dp0runtime\python\pythonw.exe" set "MAS_PYW=%~dp0runtime\python\pythonw.exe"
"%MAS_PY%" "%~dp0app\apps\desktop\launcher.py"
if errorlevel 1 pause
"@
Set-Content -Path (Join-Path $Stage "start-mas.cmd") -Value $start -Encoding ASCII

$ico = Join-Path $DesktopDir "src-tauri\icons\icon.ico"
if (Test-Path $ico) { Copy-Item $ico (Join-Path $Stage "icon.ico") -Force }

@"
Multimodal Agent Studio $Ver (Windows layout)
- Double-click start-mas.cmd
- First run opens the setup wizard (data dir / encryption / ComfyUI probe / optional API key)
- Auto-update is OFF by default
- Uninstall keeps data + keychain unless you tick the option
Build: apps/desktop/packaging/windows/build.ps1
"@ | Set-Content -Path (Join-Path $Stage "README.txt") -Encoding UTF8

Write-Host "Layout ready: $Stage"

# NSIS
if (-not $SkipNsis) {
  $makensis = Get-Command makensis -ErrorAction SilentlyContinue
  if ($makensis) {
    $nsis = Join-Path $Here "installer.nsi"
    & makensis "/DVERSION=$Ver" "/DSTAGE=$Stage" $nsis
    Write-Host "NSIS installer built under dist\"
  } else {
    Write-Warning "makensis not on PATH — skipped installer.exe. Layout above is still usable (portable)."
  }
}

Write-Host "Done."
