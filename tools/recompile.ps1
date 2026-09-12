# Quick recompile: re-exports just the game, without rebuilding the bridge.
# Use this after editing .gd scripts/scenes. Run from the repository root:
#
#   powershell -File tools/recompile.ps1
#
# If you changed anything under bridge/, use tools/build_windows.ps1 instead
# so the bridge exe gets rebuilt too.
$godot = "C:\Users\darsh\Downloads\stuff\Godot_v4.7.1-stable_win64.exe\Godot_v4.7.1-stable_win64_console.exe"

New-Item -ItemType Directory -Force build | Out-Null
& $godot --headless --path game --export-release "Windows Desktop" ..\build\BingBangDreamAdventure.exe
if ($LASTEXITCODE -ne 0) { throw "export failed ($LASTEXITCODE)" }

Write-Host "Re-exported build\BingBangDreamAdventure.exe"
