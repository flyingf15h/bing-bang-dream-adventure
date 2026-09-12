# Builds the whole build/ folder a player runs: the exported game plus the
# bridge, ready to hand to a machine with no Godot, no repo and no Python
# packages installed yet. Run from the repository root:
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

# The bridge ships as source, not as a second .exe -- it is a two-file
# dependency list away from running anywhere Python does, and freezing it
# would only add a second build step with its own way to go stale.
Copy-Item bridge\run_bridge.py build\ -Force
Copy-Item bridge\requirements.txt build\ -Force
Copy-Item bridge\bbda build\bbda -Recurse -Force
# __pycache__ is a byproduct of having run the bridge from source in this
# repo, not part of what ships -- Python regenerates it on the target
# machine, and shipping this one would just be someone else's bytecode for
# an interpreter version that may not match theirs.
Get-ChildItem build\bbda -Recurse -Directory -Filter __pycache__ |
	Remove-Item -Recurse -Force

@"
1. pip install -r requirements.txt
2. python run_bridge.py
3. run BingBangDreamAdventure.exe
"@ | Set-Content -Path build\README.txt -NoNewline

Write-Host "build\ is ready to ship."
