# Builds the whole build/ folder a player runs: the exported game plus the
# bundled bridge, ready to hand to a machine with no Godot, no repo and no
# Python at all. Run from the repository root:
#
#   powershell -File tools/build_windows.ps1
#
# Uses the _console.exe variant deliberately: the plain godot.exe detaches
# from the terminal and swallows export errors, so a failed export looks
# like a silent success. The console build blocks until export finishes and
# actually reports $LASTEXITCODE.
$godot = "C:\Users\darsh\Downloads\stuff\Godot_v4.7.1-stable_win64.exe\Godot_v4.7.1-stable_win64_console.exe"

New-Item -ItemType Directory -Force build | Out-Null
& $godot --headless --path game --export-release "Windows Desktop" ..\build\BingBangDreamAdventure.exe
if ($LASTEXITCODE -ne 0) { throw "export failed ($LASTEXITCODE)" }

Write-Host "Exported build\BingBangDreamAdventure.exe"

# Bundled rather than shipped as source: the game launches build\bridge\
# bridge.exe itself (see game/autoload/BridgeLauncher.gd) the moment it
# starts, and killing it again on quit. That only works unattended if
# nothing about starting it needs a person to have run pip first.
powershell -File (Join-Path $PSScriptRoot "build_bridge.ps1")
if ($LASTEXITCODE -ne 0) { throw "bridge build failed ($LASTEXITCODE)" }

@"
Plug in your controller(s), then run BingBangDreamAdventure.exe.

The bridge (bridge\bridge.exe) starts and stops with the game -- nothing
else to install, nothing else to run. If a board is not being found, check
bridge\bridge.log, written next to bridge.exe each time it runs.
"@ | Set-Content -Path build\README.txt -NoNewline

Write-Host "build\ is ready to ship."
