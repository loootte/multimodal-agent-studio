# Copy release exe into apps/desktop/dist for easy double-click.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "src-tauri\target\release\mas-desktop.exe"
$dist = Join-Path $root "dist"
if (-not (Test-Path $src)) {
  Write-Error "Missing $src - run npm run build in apps/desktop first"
}
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$dst = Join-Path $dist "MultimodalAgentStudio.exe"
Copy-Item -Force $src $dst
Write-Host "Copied to $dst"
$nsisDir = Join-Path $root "src-tauri\target\release\bundle\nsis"
if (Test-Path $nsisDir) {
  $nsis = Get-ChildItem (Join-Path $nsisDir "*.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($nsis) {
    Copy-Item -Force $nsis.FullName (Join-Path $dist $nsis.Name)
    Write-Host "Installer: $(Join-Path $dist $nsis.Name)"
  }
}
