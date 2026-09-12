# Bundles bridge/run_bridge.py + bbda/ into build/bridge/ -- no Python
# install needed on the machine that runs the game. Run from the repository
# root: powershell -File tools/build_bridge.ps1
#
# This is a build-time step only. It needs PyInstaller on *this* machine
# (pip install pyinstaller); nothing it produces needs Python on the machine
# it ships to -- numpy, pyserial and the whole bbda package are frozen in.
#
# --onedir, deliberately, not --onefile. A single-file build is a bootloader
# that extracts itself to a temp directory and re-execs the real program as
# a *child* process -- fine for something a person launches by hand, wrong
# for something the game launches and later has to kill by PID: killing the
# bootloader leaves the actual worker running, orphaned, still holding the
# control port, invisible, and the next launch looks like the game is
# broken. --onedir's bridge.exe *is* the worker; the PID Godot gets back is
# the PID that goes away when killed.
$ErrorActionPreference = "Stop"

Push-Location bridge
try {
	python -m PyInstaller --onedir --name bridge `
		--distpath dist --workpath build_pyinstaller --specpath . `
		run_bridge.py
	if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
} finally {
	Pop-Location
}

New-Item -ItemType Directory -Force build\bridge | Out-Null
Copy-Item bridge\dist\bridge\* build\bridge\ -Recurse -Force

Write-Host "Built build\bridge\bridge.exe"
