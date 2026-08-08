"""Entry point: python main.py [--port COM7 | --host 192.168.1.50[:3333]]"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from bbda.app import Dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description="ICM-45605 + QMC6309 dashboard")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--port", help="serial port to open on startup, e.g. COM7")
    target.add_argument(
        "--host",
        help="board address to stream from over UDP, e.g. 192.168.1.50 or "
             "192.168.1.50:3333",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("bbda-dashboard")

    window = Dashboard()
    window.show()

    if args.port:
        index = window.port_combo.findData(args.port)
        if index >= 0:
            window.port_combo.setCurrentIndex(index)
            window._toggle_connection()
        else:
            print(f"Port {args.port} not found; pick one from the dropdown.")
    elif args.host:
        host, _, port = args.host.partition(":")
        window.transport_combo.setCurrentIndex(1)
        window.host_input.setText(host)
        if port:
            window.udp_port_input.setText(port)
        window._toggle_connection()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
