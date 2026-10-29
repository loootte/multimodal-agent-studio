# Copy release exe into apps/desktop/dist for easy double-click.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "src-tauri\target\release\mas-desktop.exe"
$dist = Join-Path $root "dist"
if (-not (Test-Path $src)) {
  Write-Error "缺少 $src ，请先在 apps/desktop 运行 npm run build"
}
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$dst = Join-Path $dist "MultimodalAgentStudio.exe"
Copy-Item -Force $src $dst
Write-Host "已复制到 $dst"
# Also copy NSIS installer if present
$nsis = Get-ChildItem (Join-Path $root "src-tauri\target\release\bundle\nsis\*.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
if ($nsis) {
  Copy-Item -Force $nsis.FullName (Join-Path $dist $nsis.Name)
  Write-Host "安装包: $(Join-Path $dist $nsis.Name)"
}
