#!/usr/bin/env bash
# Installs the udev rule that lets the bridge open the board's serial port
# without root and keeps ModemManager off it. Run once per machine:
#
#   sudo ./install_udev.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" -ne 0 ]; then
	exec sudo "$0" "$@"
fi
install -m 0644 "$here/99-bbda.rules" /etc/udev/rules.d/99-bbda.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --subsystem-match=usb
echo "Installed /etc/udev/rules.d/99-bbda.rules -- unplug and replug the board."
