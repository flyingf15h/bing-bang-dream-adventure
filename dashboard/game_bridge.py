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
import math
import sys
import threading
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


#: Frames per second the demo animates its fake board at, which is also the
#: rate a real board's motion records arrive at.
DEMO_FPS = 30.0

#: How long one lane takes, start of the wind-up to rest again.
DEMO_PERIOD_S = 0.8

#: How long the swing itself lasts, rise to fall. Around what a hand does, and
#: comfortably longer than the detector's refractory period so consecutive
#: simulated flicks are not swallowed as one.
SIM_SWING_S = 0.5

#: Period for flicks injected into a live board session, longer than the demo's
#: so there is a clear gap in which the real board's own motion shows through.
SIM_PERIOD_S = 1.2


def gesture_frames(config: BridgeConfig,
                   period_s: float) -> list[tuple[float, bool]]:
    """One simulated flick as (swing dps, send the flick now) per frame.

    The swing rises to a peak and falls back to nothing, and the flick is
    published at the peak -- which is where a real detector puts it, since the
    bearing is read from the fastest part of the movement. The frames after
    the swing are the board back at rest, and they matter as much as the swing
    does: they are what proves the arrow retracts rather than sticking.
    """
    frames = max(2, int(round(period_s * DEMO_FPS)))
    swing_frames = max(2, int(round(SIM_SWING_S * DEMO_FPS)))
    peak_frame = max(1, swing_frames // 2)
    peak_dps = config.on_threshold_dps * 2.5
    return [
        (peak_dps * math.sin(math.pi * frame / swing_frames)
         if frame <= swing_frames else 0.0,
         frame == peak_frame)
        for frame in range(frames)
    ]


def play_gesture(bridge: GameBridge, config: BridgeConfig, bearing: float,
                 frames: list[tuple[float, bool]],
                 mute_board: bool = False) -> None:
    """Send one simulated gesture in real time, a frame at a time."""
    swing_s = min(SIM_SWING_S, len(frames) / DEMO_FPS)
    for swing, is_flick in frames:
        bridge.poll_control()
        if mute_board and swing > 0.0:
            # Hold the board's own motion off the wire for as long as the
            # simulated swing lasts, so the two do not overwrite each other 30
            # times a second. Renewed per frame rather than set once, so that
            # killing the simulator never leaves the real board muted.
            bridge.mute_motion_until = time.monotonic() + swing_s
        if is_flick:
            bridge.send_demo_flick(bearing)
        if config.motion_hz > 0.0:
            bridge.send_demo_motion(bearing, swing)
        time.sleep(1.0 / DEMO_FPS)


def lane_bearings(config: BridgeConfig) -> list[float]:
    """The bearing at the centre of each lane, in order."""
    return [(config.sector_offset_deg + i * (360.0 / config.sectors)) % 360.0
            for i in range(config.sectors)]


def run_demo(bridge: GameBridge, config: BridgeConfig) -> int:
    """Send a flick into each lane in turn, round and round.

    The bearings are the *centres* of the detector's sectors, not multiples of
    60. A bearing of 0 -- straight up -- sits exactly between two of the game's
    lanes, so a demo built on multiples of 60 lands every flick on a boundary,
    hits one lane twice and another never, and looks like a mapping bug when it
    is only an unlucky choice of test angles.

    Each flick is wrapped in a rise and fall of motion records, so this drives
    the game's IMU arrow the way a hand would: it swings up, the flick fires at
    the top of the swing, and it settles back. Without that the demo would only
    exercise the flick half of the link and an arrow that never moved would
    look like a working one.
    """
    half = 360.0 / (2 * config.sectors)
    bearings = lane_bearings(config)
    print(f"Demo: flicks to {bridge.peer[0]}:{bridge.peer[1]}, Ctrl-C to stop.")
    print(f"      {config.sectors} lane centres, {half * 2:.0f} degrees apart.")
    if config.motion_hz > 0.0:
        print(f"      plus {DEMO_FPS:.0f} Hz of motion, to move the arrow.")
    print()
    bridge.open_control()
    bridge.send_hello("demo", "none")
    bridge.send_config()

    frames = gesture_frames(config, DEMO_PERIOD_S)
    index = 0
    try:
        while True:
            bearing = bearings[index % len(bearings)]
            print(f"  lane {index % len(bearings) + 1}: bearing {bearing:5.1f} deg")
            play_gesture(bridge, config, bearing, frames)
            index += 1
    except KeyboardInterrupt:
        print("\nStopped.")
        bridge.close()
    return 0


def simulate_flicks(bridge: GameBridge, config: BridgeConfig,
                    stop: threading.Event) -> None:
    """Inject simulated flicks while a real board is streaming.

    For testing the game against real hardware without a free hand: the board
    is connected, sampling and detecting as usual, and lane after lane is
    flicked for it. Anything the board itself detects still goes through --
    only the live motion is taken over, and only for as long as each simulated
    swing lasts, so picking the board up between them still moves the arrow.

    Run on a daemon thread so Ctrl-C reaches the main loop the way it does
    without this, and left as a testing aid rather than a menu item: a flick
    the player did not make is a hit they did not earn.
    """
    bearings = lane_bearings(config)
    frames = gesture_frames(config, SIM_PERIOD_S)
    index = 0
    while not stop.is_set():
        bearing = bearings[index % len(bearings)]
        print(f"[sim] lane {index % len(bearings) + 1}: bearing {bearing:5.1f} deg")
        play_gesture(bridge, config, bearing, frames, mute_board=True)
        index += 1


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
    # Read off BridgeConfig rather than written out again, for the same reason
    # the tuning defaults below are: a flag whose default has drifted from what
    # the bridge runs when the flag is absent is worse than no flag at all.
    stock_rate = BridgeConfig().rate_hz

    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--demo", action="store_true",
                        help="send fake flicks without a board, to test the game")
    parser.add_argument("--simulate-flicks", action="store_true",
                        help="with a real board connected, also flick every "
                             "lane in turn, to test the game hands-free")

    parser.add_argument("--game-host", default="127.0.0.1",
                        help="where the game listens (default: %(default)s)")
    parser.add_argument("--game-port", type=int, default=DEFAULT_GAME_PORT,
                        help="game UDP port (default: %(default)s)")
    parser.add_argument("--control-port", type=int, default=None,
                        help="where the in-game debug panel sends settings "
                             "(default: one above --game-port)")

    parser.add_argument("--baud", type=int, default=921600,
                        help="serial baud; ignored by native USB CDC (default: %(default)s)")
    parser.add_argument("--rate", type=int, default=stock_rate,
                        help="samples per second to ask the board for (default: %(default)s)")
    parser.add_argument("--raw", action="store_true",
                        help="do not apply the board's stored calibration")
    parser.add_argument("--motion-hz", type=float, default=30.0,
                        help="rate for the live records the game's IMU arrow "
                             "follows; 0 sends none (default: %(default)s)")

    # Every default here comes off BridgeConfig rather than being written out
    # again, so the flags cannot drift from what the bridge actually runs when
    # none of them are passed -- which is the usual case.
    stock = BridgeConfig()
    tuning = parser.add_argument_group("flick tuning")
    tuning.add_argument("--threshold", type=float, default=stock.on_threshold_dps,
                        help="dps that starts a flick (default: %(default)s)")
    tuning.add_argument("--swing", type=float, default=stock.min_swing,
                        help="0..1 floor on how much of the rotation was a swing "
                             "rather than a roll (default: %(default)s)")
    tuning.add_argument("--margin", type=float, default=stock.min_margin,
                        help="0..1 floor on distance from a sector boundary "
                             "(default: %(default)s)")
    tuning.add_argument("--refractory", type=float, default=stock.refractory_ms,
                        help="ms to ignore after a flick, which is what stops the "
                             "return stroke registering (default: %(default)s)")
    tuning.add_argument("--sectors", type=int, default=stock.sectors,
                        help="lanes the game has (default: %(default)s)")
    tuning.add_argument("--sector-offset", type=float, default=stock.sector_offset_deg,
                        help="degrees the sector grid is rotated by (default: %(default)s)")
    tuning.add_argument("--front", default=stock.front,
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
        control_port=(args.control_port if args.control_port is not None
                      else args.game_port + 1),
        rate_hz=args.rate,
        calibrated=not args.raw,
        on_threshold_dps=args.threshold,
        min_swing=args.swing,
        min_margin=args.margin,
        refractory_ms=args.refractory,
        sectors=args.sectors,
        sector_offset_deg=args.sector_offset,
        front=args.front,
        motion_hz=args.motion_hz,
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

    bridge.open_control()

    stop_simulator = threading.Event()
    if args.simulate_flicks:
        print("Simulating a flick into each lane in turn, alongside the board.")
        print("Real flicks still count; only the live arrow is taken over.\n")
        threading.Thread(target=simulate_flicks,
                         args=(bridge, config, stop_simulator),
                         daemon=True).start()

    try:
        _serve(bridge, args, host, udp_port)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop_simulator.set()
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
        # Short enough that the in-game panel feels connected to what it is
        # editing: this is how often a slider being dragged is acted on, and a
        # half-second lag there reads as the control having no effect.
        time.sleep(0.05)
        bridge.poll_control()
        bridge.expire_bias_write()
        now = time.monotonic()

        if args.monitor and bridge.connected and now - last_monitor >= 0.5:
            last_monitor = now
            threshold = bridge.config.on_threshold_dps
            peak = bridge.peak_dps_seen
            bridge.peak_dps_seen = 0.0
            transport = bridge.peak_transport_ms
            bridge.peak_transport_ms = 0.0
            bar = "#" * min(40, int(40.0 * peak / max(threshold * 2.0, 1.0)))
            verdict = "over" if peak >= threshold else "under"
            # Transport delay alongside the rate, because they are the two
            # halves of "did that flick register properly": whether it was
            # strong enough to be seen at all, and how stale it was by the time
            # it was. A board can be perfectly detectable and still arrive too
            # late to score, and nothing else in this tool would say so.
            print(f"  |gyro| peak {peak:7.1f} dps  {verdict:<5} "
                  f"{threshold:.0f}  |{bar:<40}|  wire {transport:5.1f} ms")

        if not bridge.connected:
            print("[link] board gone -- looking for it again")
            print("       NOTHING YOU DO WITH THE BOARD WILL REGISTER until "
                  "it is back.")
            # Repeated rather than said once. The wait is open-ended, and with
            # --simulate-flicks scrolling past it, a single line an hour ago is
            # exactly how someone ends up flicking at a board that is not
            # plugged in and concluding that flick detection is broken.
            waiting_since = now
            said = now
            while True:
                # Polled while waiting too: the panel is the most likely place
                # someone is watching from when the board has gone away, and a
                # panel whose controls stop responding looks like a second
                # fault on top of the first.
                for _ in range(20):
                    time.sleep(0.1)
                    bridge.poll_control()
                if time.monotonic() - said >= 10.0:
                    said = time.monotonic()
                    print(f"[link] still no board after "
                          f"{said - waiting_since:.0f}s. On an ESP32-S3 the "
                          f"port is provided by the running sketch, so a reset "
                          f"or a power-only cable makes it vanish like this.")
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
