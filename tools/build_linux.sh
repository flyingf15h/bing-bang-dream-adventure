#!/usr/bin/env bash
# Linux counterpart of tools/build_windows.ps1: builds build-linux/, the
# exported game plus the bundled bridge, ready for a machine with no Godot,
# no repo and no Python. Run from anywhere:
#
#   tools/build_linux.sh              # godot on PATH
#   GODOT=/path/to/godot tools/build_linux.sh
#   tools/build_linux.sh --game-only  # skip the bridge, like recompile.ps1
#
# Needs the Godot 4.7.1 export templates installed, and for the bridge, a
# Python with `pip install -r bridge/requirements.txt pyinstaller`
# (override which with PYTHON=...).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
godot="${GODOT:-godot}"
python="${PYTHON:-python3}"
out="build-linux"

mkdir -p "$out"
"$godot" --headless --path game --export-release "Linux" "../$out/BingBangDreamAdventure.x86_64"
# Godot can exit 0 having written nothing when a template is missing.
[ -x "$out/BingBangDreamAdventure.x86_64" ] || { echo "export failed" >&2; exit 1; }
echo "Exported $out/BingBangDreamAdventure.x86_64"

if [ "${1:-}" = "--game-only" ]; then
	exit 0
fi

# --onedir for the same reason as build_bridge.ps1: the game kills the bridge
# by PID, and a --onefile build's PID is a bootloader whose real worker
# outlives it.
(
	cd bridge
	"$python" -m PyInstaller --noconfirm --onedir --name bridge \
		--distpath dist --workpath build_pyinstaller --specpath . \
		run_bridge.py
)
rm -rf "$out/bridge"
cp -r bridge/dist/bridge "$out/bridge"
echo "Built $out/bridge/bridge"

cp tools/linux/99-bbda.rules tools/linux/install_udev.sh "$out/"
cat > "$out/README.txt" <<'TXT'
First time only: let the game talk to the controllers over USB without root.

    sudo ./install_udev.sh

then unplug and replug the board(s).

Plug in your controller(s), then run ./BingBangDreamAdventure.x86_64.

The bridge (bridge/bridge) starts and stops with the game -- nothing else to
install, nothing else to run. If a board is not being found, check
bridge/bridge.log, written next to the bridge each time it runs.
TXT

echo "$out/ is ready to ship."
