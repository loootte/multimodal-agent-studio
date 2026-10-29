# Build a double-clickable launcher exe with PyInstaller (no Rust required).
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
$desktop = Join-Path $root "apps\desktop"
Set-Location $desktop
$py = $env:MAS_PYTHON
if (-not $py) { $py = "python" }
& $py -m pip install -q --upgrade pip
& $py -m pip install -q pywebview pyinstaller
$dist = Join-Path $desktop "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$ico = Join-Path $desktop "src-tauri\icons\icon.ico"
$args = @(
  "-m", "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--windowed",
  "--name", "MultimodalAgentStudio",
  "--distpath", $dist,
  "--workpath", (Join-Path $desktop "build\launcher-work"),
  "--specpath", (Join-Path $desktop "build")
)
if (Test-Path $ico) { $args += @("--icon", $ico) }
$args += (Join-Path $desktop "launcher.py")
& $py @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
$exe = Join-Path $dist "MultimodalAgentStudio.exe"
Write-Host "exe: $exe"
Write-Host "NOTE: pywebview launcher (not Tauri). Needs local Python + repo sources."
