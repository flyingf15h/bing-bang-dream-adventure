"""Feeds the Godot game IMU flicks, over USB serial or over WiFi.

    python game_bridge.py                       # find the board on USB
    python game_bridge.py --port COM7           # a particular serial port
    python game_bridge.py --host 192.168.1.50   # over WiFi instead
    python game_bridge.py --list                # what serial ports exist
    python game_bridge.py --demo                # no board: fake flicks

Leave it running alongside the game. It prints a line per flick with -v, which
is the quickest way to tell whether a flick that did not register was missed by
the detector or lost between here and the game.

See dashboard/bbda/gamebridge.py for the wire format and the reasoning.
"""

from __future__ import annotations

import argparse
import sys
import time

from bbda.gamebridge import (
    DEFAULT_GAME_PORT,
    BridgeConfig,
    GameBridge,
    make_link,
)


def list_ports() -> int:
    from serial.tools import list_ports as tools

    ports = list(tools.comports())
    if not ports:
        print("No serial ports at all.")
        print()
        print("On an ESP32-S3 with 'USB CDC On Boot' enabled the port only")
        print("appears once the sketch is running -- it is the sketch that")
        print("provides it, not a bridge chip. If nothing shows up: check the")
        print("cable carries data rather than power only, and re-flash while")
        print("holding BOOT if the firmware is not running.")
        return 1

    print(f"{len(ports)} serial port(s):")
    for port in ports:
        vid = f"{port.vid:04X}" if port.vid is not None else "----"
        pid = f"{port.pid:04X}" if port.pid is not None else "----"
        mark = "  <- looks like the board" if port.vid == 0x303A else ""
        print(f"  {port.device:<8} {vid}:{pid}  {port.description}{mark}")
    return 0


def run_demo(bridge: GameBridge, config: BridgeConfig) -> int:
    """Send a flick into each lane in turn, round and round.

    The bearings are the *centres* of the detector's sectors, not multiples of
    60. A bearing of 0 -- straight up -- sits exactly between two of the game's
    lanes, so a demo built on multiples of 60 lands every flick on a boundary,
    hits one lane twice and another never, and looks like a mapping bug when it
    is only an unlucky choice of test angles.
    """
    half = 360.0 / (2 * config.sectors)
    bearings = [config.sector_offset_deg + i * (360.0 / config.sectors)
                for i in range(config.sectors)]
    print(f"Demo: flicks to {bridge.peer[0]}:{bridge.peer[1]}, Ctrl-C to stop.")
    print(f"      {config.sectors} lane centres, {half * 2:.0f} degrees apart.\n")
    bridge.send_hello("demo", "none")
    index = 0
    try:
        while True:
            bearing = bearings[index % len(bearings)] % 360.0
            bridge.send_demo_flick(bearing)
            print(f"  lane {index % len(bearings) + 1}: bearing {bearing:5.1f} deg")
            index += 1
            time.sleep(0.8)
    except KeyboardInterrupt:
        print("\nStopped.")
        bridge.close()
    return 0


def main() -> int:
    # Python block-buffers stdout when it is not a terminal, which for this
    # tool defeats the point: the usual way to keep a record of a session is
    # to pipe it to a file or a log window, and a flick log that appears in
    # 8 KB lumps minutes later cannot be matched against what the board was
    # doing at the time.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="Bridge the board's IMU flicks into the Godot game.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--port", help="serial port, e.g. COM7 (default: find it)")
    transport.add_argument(
        "--host",
        help="board IP for WiFi, e.g. 192.168.1.50 or 192.168.1.50:3333",
    )
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--demo", action="store_true",
                        help="send fake flicks without a board, to test the game")

    parser.add_argument("--game-host", default="127.0.0.1",
                        help="where the game listens (default: %(default)s)")
    parser.add_argument("--game-port", type=int, default=DEFAULT_GAME_PORT,
                        help="game UDP port (default: %(default)s)")

    parser.add_argument("--baud", type=int, default=921600,
                        help="serial baud; ignored by native USB CDC (default: %(default)s)")
    parser.add_argument("--rate", type=int, default=200,
                        help="samples per second to ask the board for (default: %(default)s)")
    parser.add_argument("--raw", action="store_true",
                        help="do not apply the board's stored calibration")

    tuning = parser.add_argument_group("flick tuning")
    tuning.add_argument("--threshold", type=float, default=150.0,
                        help="dps that starts a flick (default: %(default)s)")
    tuning.add_argument("--swing", type=float, default=0.6,
                        help="0..1 floor on how much of the rotation was a swing "
                             "rather than a roll (default: %(default)s)")
    tuning.add_argument("--margin", type=float, default=0.15,
                        help="0..1 floor on distance from a sector boundary "
                             "(default: %(default)s)")
    tuning.add_argument("--refractory", type=float, default=200.0,
                        help="ms to ignore after a flick, which is what stops the "
                             "return stroke registering (default: %(default)s)")
    tuning.add_argument("--sectors", type=int, default=6,
                        help="lanes the game has (default: %(default)s)")
    tuning.add_argument("--sector-offset", type=float, default=30.0,
                        help="degrees the sector grid is rotated by (default: %(default)s)")
    tuning.add_argument("--front", default="+X",
                        choices=["+X", "-X", "+Y", "-Y", "+Z", "-Z"],
                        help="board axis pointing away from the player "
                             "(default: %(default)s)")

    parser.add_argument("--monitor", action="store_true",
                        help="also show live rotation rate against the threshold, "
                             "to see whether a flick is strong enough to count")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="do not print a line per flick")
    args = parser.parse_args()

    if args.list:
        return list_ports()

    config = BridgeConfig(
        game_host=args.game_host,
        game_port=args.game_port,
        rate_hz=args.rate,
        calibrated=not args.raw,
        on_threshold_dps=args.threshold,
        min_swing=args.swing,
        min_margin=args.margin,
        refractory_ms=args.refractory,
        sectors=args.sectors,
        sector_offset_deg=args.sector_offset,
        front=args.front,
        verbose=not args.quiet,
    )
    bridge = GameBridge(config)

    if args.demo:
        return run_demo(bridge, config)

    udp_port = 3333
    host = args.host
    if host and ":" in host:
        host, _, port_text = host.rpartition(":")
        try:
            udp_port = int(port_text)
        except ValueError:
            host = args.host

    try:
        link, target = make_link(
            port=args.port, host=host, baud=args.baud, udp_port=udp_port
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    kind = "WiFi" if args.host else "USB serial"
    print(f"Board:  {target}  ({kind})")
    print(f"Game:   {config.game_host}:{config.game_port}")
    if not args.port and not args.host:
        print("        (auto-detected; --port overrides, --list shows all)")
    print("Ctrl-C to stop.\n")

    # Retry the first connection rather than giving up on it. A board that is
    # mid-reset has no serial port for a second or two, and exiting then means
    # the bridge has to be started by hand at exactly the right moment -- the
    # one thing a background helper should never ask of anybody.
    attempt = 0
    while not bridge.open(link, target):
        attempt += 1
        if attempt == 1:
            print("Could not open the board yet -- retrying, Ctrl-C to give up.")
            if not args.host:
                print("  * `--list` shows which ports exist.")
                print("  * A port that exists but will not open is usually "
                      "held by another program -- the dashboard, or a serial "
                      "monitor.")
            else:
                print("  * `wifi status` on the board over USB prints its IP.")
        try:
            time.sleep(2.0)
            link, target = make_link(
                port=args.port, host=host, baud=args.baud, udp_port=udp_port
            )
        except RuntimeError:
            continue        # the port has not come back yet
        except KeyboardInterrupt:
            print("\nStopping.")
            return 1
    if attempt:
        print(f"Connected on {target} after {attempt} retries.\n")

    try:
        _serve(bridge, args, host, udp_port)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        bridge.close()
    return 0


def _serve(bridge: GameBridge, args, host: str | None, udp_port: int) -> None:
    """Keep the board's stream flowing into the game until interrupted.

    Reconnects rather than exiting, because on a board with native USB the
    serial port is provided by the running sketch: reset the board and the
    port disappears from the system and comes back moments later, sometimes
    under a different COM number. That is not a failure, it is what a reset
    looks like from here -- and a game input device that has to be restarted
    by hand every time the board is power-cycled is not one anybody would use.
    So a serial reconnect re-runs the search rather than reopening the name it
    had before.
    """
    last_report = time.monotonic()
    last_monitor = last_report
    quiet_since: float | None = None

    while True:
        time.sleep(0.5)
        now = time.monotonic()

        if args.monitor and bridge.connected and now - last_monitor >= 0.5:
            last_monitor = now
            threshold = bridge.config.on_threshold_dps
            peak = bridge.peak_dps_seen
            bridge.peak_dps_seen = 0.0
            bar = "#" * min(40, int(40.0 * peak / max(threshold * 2.0, 1.0)))
            verdict = "over" if peak >= threshold else "under"
            print(f"  |gyro| peak {peak:7.1f} dps  {verdict:<5} "
                  f"{threshold:.0f}  |{bar:<40}|")

        if not bridge.connected:
            print("[link] board gone -- looking for it again")
            while True:
                time.sleep(2.0)
                try:
                    link, target = make_link(
                        port=args.port, host=host, baud=args.baud,
                        udp_port=udp_port,
                    )
                except RuntimeError:
                    continue        # not back yet; the port has not returned
                if bridge.open(link, target):
                    print(f"[link] reconnected on {target}")
                    quiet_since = None
                    break
            continue

        if now - last_report < 5.0:
            continue
        last_report = now
        print(
            f"[link] {bridge.sample_rate:.0f} Hz  "
            f"{bridge.samples} samples  {bridge.flicks} flicks"
        )

        # An open port that delivers nothing is its own failure, and a
        # different one from a port that will not open. Say so rather than
        # printing a reassuring 0 Hz for ever.
        if bridge.sample_rate >= 1.0:
            quiet_since = None
            continue
        if quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= 10.0:
            quiet_since = now
            print("       the link is open but no samples are arriving.")
            print("       * `mode csv` may have been turned off -- the "
                  "dashboard sends `mode pretty` when it disconnects.")
            print("       * over WiFi the board streams to whoever spoke to "
                  "it last; the dashboard may have taken it over.")
            print("       See docs/GAME_INPUT.md for the rest.")


if __name__ == "__main__":
    sys.exit(main())
