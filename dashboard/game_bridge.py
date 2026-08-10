"""Feeds the Godot game IMU flicks, over USB serial or over WiFi.

    python game_bridge.py                       # find the board on USB
    python game_bridge.py --port COM7           # a particular serial port
    python game_bridge.py --host 192.168.1.50   # over WiFi instead
    python game_bridge.py --list                # what serial ports exist
    python game_bridge.py --demo                # no board: fake flicks

Two boards, one per note colour:

    python game_bridge.py --board left=COM7 --board right=COM9
    python game_bridge.py --board blue=COM7:+Y --board pink=COM9:-X
    python game_bridge.py --two-boards          # find both, left is the first

The blue notes are the left hand and the pink ones are the right, which is what
the charts already call them. A board given a hand may only hit notes of that
colour; notes marked `any`, and the gold bonus notes, are open to either. One
board with no hand named plays everything, exactly as before.

Both boards run in this one process and post to the same game port, because
the game has one input path and one set of detection tuning, and splitting
either would mean keeping two copies of the part hardest to keep in step. Each
board keeps its own detector, its own front axis and its own control port.

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
    find_all_board_ports,
    make_link,
    normalise_hand,
)

#: An axis name, as it may be tacked onto a --board target.
AXIS_NAMES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


def parse_board(text: str) -> tuple[str, str, str | None]:
    """``left=COM7`` or ``blue=192.168.1.5:+Y`` -> (hand, target, front).

    The front axis is optional and is recognised by its shape rather than by
    its position, so that a WiFi target keeping its own ``host:port`` colon
    stays unambiguous: only a trailing piece that is literally one of the six
    axis names is taken as the axis, and anything else belongs to the target.
    """
    hand_text, separator, target = text.partition("=")
    if not separator:
        raise ValueError(
            f"--board wants hand=target, for example left=COM7; got {text!r}")
    hand = normalise_hand(hand_text)
    front = None
    head, colon, tail = target.rpartition(":")
    if colon and tail.upper() in AXIS_NAMES:
        front = tail.upper()
        target = head
    if not target:
        raise ValueError(f"--board {text!r} names no port or address")
    return hand, target, front


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


#: What each hand plays, for saying out loud. The notes are blue and pink on
#: screen and left and right in the charts, and somebody reading this line is
#: looking at the screen.
COLOUR_WORDS = {"left": "the blue notes", "right": "the pink notes",
                "": "every note"}


def split_host(target: str) -> tuple[str, int]:
    """``192.168.1.5:3333`` -> (host, port), with the board's default port."""
    if ":" in target:
        head, _, tail = target.rpartition(":")
        try:
            return head, int(tail)
        except ValueError:
            pass
    return target, 3333


def plan_boards(args, parser) -> list[tuple[str, str, str | None, str | None, int]]:
    """Work out which boards to open, as (hand, target, front, host, udp_port).

    Three ways in, and they are mutually exclusive because mixing them can only
    express something one of them already says more clearly:
    ``--board`` names each board and its colour, ``--two-boards`` finds two and
    assigns them in the order the system lists them, and the original
    ``--port`` / ``--host`` / nothing-at-all opens one board that plays
    everything.
    """
    if args.board:
        planned = []
        for text in args.board:
            try:
                hand, target, front = parse_board(text)
            except ValueError as exc:
                parser.error(str(exc))
            # A dotted address or an explicit port is WiFi; anything else is a
            # serial port name, which differs enough by platform (COM7,
            # /dev/ttyACM0, /dev/cu.usbmodem…) that sniffing it is hopeless.
            is_host = target.count(".") >= 3 or target.startswith("[")
            host, udp_port = split_host(target) if is_host else (None, 3333)
            planned.append((hand, host or target, front, host, udp_port))
        hands = [hand for hand, *_ in planned]
        if len(set(hands)) != len(hands):
            parser.error("two --board entries name the same hand; one board "
                         "plays the blue notes and one plays the pink")
        return planned

    if args.two_boards:
        found = find_all_board_ports()
        if len(found) < 2:
            parser.error(
                f"--two-boards needs two boards and found {len(found)}"
                + (f" ({found[0]})" if found else "")
                + ". `--list` shows every port; on an ESP32-S3 a port only "
                  "appears once its sketch is running, so a board that is "
                  "mid-reset or on a power-only cable will not be there. "
                  "`--board left=COM7 --board right=COM9` names them outright.")
        # First listed gets the blue notes. Arbitrary, and said out loud when
        # the boards are announced, because the alternative is the player
        # discovering it by having every note score against the wrong colour.
        return [("left", found[0], None, None, 3333),
                ("right", found[1], None, None, 3333)]

    host, udp_port = (split_host(args.host) if args.host else (None, 3333))
    if host:
        return [("", host, None, host, udp_port)]
    if args.port:
        return [("", args.port, None, None, 3333)]
    return [("", "", None, None, 3333)]     # empty target: find it


def connect(bridge: GameBridge, args, target: str, host: str | None,
            udp_port: int) -> bool:
    """Open one board, retrying rather than giving up on it.

    A board that is mid-reset has no serial port for a second or two, and
    exiting then means the bridge has to be started by hand at exactly the
    right moment -- the one thing a background helper should never ask of
    anybody.
    """
    attempt = 0
    while True:
        try:
            link, opened = make_link(port=target or None, host=host,
                                     baud=args.baud, udp_port=udp_port)
        except RuntimeError as exc:
            if attempt == 0:
                print(f"        {exc}")
        else:
            if bridge.open(link, opened):
                if attempt:
                    print(f"        connected after {attempt} retries")
                return True
        attempt += 1
        if attempt == 1:
            print("        not open yet -- retrying, Ctrl-C to give up.")
            if not host:
                print("        * `--list` shows which ports exist.")
                print("        * A port that exists but will not open is "
                      "usually held by another program -- the dashboard, or "
                      "a serial monitor.")
            else:
                print("        * `wifi status` on the board over USB prints "
                      "its IP.")
        try:
            time.sleep(2.0)
        except KeyboardInterrupt:
            print("\nStopping.")
            return False


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
    transport.add_argument(
        "--board", action="append", metavar="HAND=TARGET[:FRONT]", default=[],
        help="a board and the note colour it plays, repeatable: "
             "left=COM7 (blue notes), right=COM9 (pink), "
             "blue=192.168.1.5:+Y to pin a front axis or use WiFi")
    transport.add_argument(
        "--two-boards", action="store_true",
        help="find two boards on USB and give the first the blue notes")
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

    boards = plan_boards(args, parser)

    stock = BridgeConfig()

    def build(hand: str, front: str, index: int) -> BridgeConfig:
        """Detection tuning is shared; the board-specific parts are not.

        Thresholds, margins and the sector layout describe what a flick *is*,
        which is the same question whichever hand threw it -- so both boards
        get the flags as given. The front axis and the hand describe this
        particular board, and the control port has to differ or the second
        bridge silently fails to bind and its half of the panel does nothing.
        """
        return BridgeConfig(
            game_host=args.game_host,
            game_port=args.game_port,
            control_port=base_control + index,
            rate_hz=args.rate,
            calibrated=not args.raw,
            on_threshold_dps=args.threshold,
            min_swing=args.swing,
            min_margin=args.margin,
            refractory_ms=args.refractory,
            sectors=args.sectors,
            sector_offset_deg=args.sector_offset,
            front=front,
            hand=hand,
            motion_hz=args.motion_hz,
            verbose=not args.quiet,
        )

    base_control = (args.control_port if args.control_port is not None
                    else args.game_port + 1)

    if args.demo:
        return run_demo(GameBridge(build("", args.front, 0)), stock)

    print(f"Game:   {args.game_host}:{args.game_port}")
    running: list[GameBridge] = []
    for index, (hand, target, front, host, udp_port) in enumerate(boards):
        config = build(hand, front or args.front, index)
        bridge = GameBridge(config)
        kind = "WiFi" if host else "USB serial"
        plays = COLOUR_WORDS.get(hand, "every note")
        print(f"Board:  {target:<22} ({kind})  plays {plays}"
              + (f"  front {config.front}" if len(boards) > 1 else ""))
        bridge.reconnect_target = (host, udp_port)
        if not connect(bridge, args, target, host, udp_port):
            return 1
        bridge.open_control()
        running.append(bridge)
    print("Ctrl-C to stop.\n")

    stop_simulator = threading.Event()
    if args.simulate_flicks:
        print("Simulating a flick into each lane in turn, alongside the board.")
        print("Real flicks still count; only the live arrow is taken over.\n")
        threading.Thread(target=simulate_flicks,
                         args=(running[0], running[0].config, stop_simulator),
                         daemon=True).start()

    try:
        _serve(running, args)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop_simulator.set()
        for bridge in running:
            bridge.close()
    return 0


def _label(bridge: GameBridge) -> str:
    """How to name one board in a log line shared by two of them."""
    hand = bridge.config.hand
    return "link" if not hand else ("blue" if hand == "left" else "pink")


def _serve(bridges: list[GameBridge], args) -> None:
    """Keep every board's stream flowing into the game until interrupted.

    Reconnects rather than exiting, because on a board with native USB the
    serial port is provided by the running sketch: reset the board and the
    port disappears from the system and comes back moments later, sometimes
    under a different COM number. That is not a failure, it is what a reset
    looks like from here -- and a game input device that has to be restarted
    by hand every time the board is power-cycled is not one anybody would use.

    With two boards the reconnect is per board and the other one keeps
    playing. That is the whole reason this loop is not simply run twice in two
    processes: one board going away must not stop the other, and the player
    should be told which colour has stopped working rather than that "the
    board" has.
    """
    last_report = time.monotonic()
    last_monitor = last_report
    quiet_since: dict[int, float] = {}
    down_since: dict[int, float] = {}
    said_down: dict[int, float] = {}

    while True:
        # Short enough that the in-game panel feels connected to what it is
        # editing: this is how often a slider being dragged is acted on, and a
        # half-second lag there reads as the control having no effect.
        time.sleep(0.05)
        for bridge in bridges:
            bridge.poll_control()
            bridge.expire_bias_write()
        now = time.monotonic()

        if args.monitor and now - last_monitor >= 0.5:
            last_monitor = now
            for bridge in bridges:
                if not bridge.connected:
                    continue
                threshold = bridge.config.on_threshold_dps
                peak = bridge.peak_dps_seen
                bridge.peak_dps_seen = 0.0
                transport = bridge.peak_transport_ms
                bridge.peak_transport_ms = 0.0
                bar = "#" * min(40, int(40.0 * peak / max(threshold * 2.0, 1.0)))
                verdict = "over" if peak >= threshold else "under"
                # Transport delay alongside the rate, because they are the two
                # halves of "did that flick register properly": whether it was
                # strong enough to be seen at all, and how stale it was by the
                # time it was. A board can be perfectly detectable and still
                # arrive too late to score, and nothing else here would say so.
                print(f"  [{_label(bridge)}] |gyro| peak {peak:7.1f} dps  "
                      f"{verdict:<5} {threshold:.0f}  |{bar:<40}|  "
                      f"wire {transport:5.1f} ms")

        # Reconnect whatever has gone away, without blocking the boards that
        # have not. The old loop sat inside a `while True` here and waited, so
        # a second board would have gone unread -- and unread over WiFi means
        # its datagrams pile up and it comes back seconds behind.
        for index, bridge in enumerate(bridges):
            if bridge.connected:
                if index in down_since:
                    del down_since[index]
                continue
            plays = COLOUR_WORDS.get(bridge.config.hand, "every note")
            if index not in down_since:
                down_since[index] = now
                said_down[index] = now
                print(f"[{_label(bridge)}] board gone -- looking for it again")
                print(f"       NOTHING YOU DO WITH IT WILL REGISTER until it "
                      f"is back, and it plays {plays}.")
            elif now - said_down[index] >= 10.0:
                # Repeated rather than said once. The wait is open-ended, and a
                # single line a minute ago is exactly how someone ends up
                # flicking at a board that is not plugged in and concluding
                # that flick detection is broken.
                said_down[index] = now
                print(f"[{_label(bridge)}] still no board after "
                      f"{now - down_since[index]:.0f}s. On an ESP32-S3 the "
                      f"port is provided by the running sketch, so a reset or "
                      f"a power-only cable makes it vanish like this.")
            host, udp_port = bridge.reconnect_target
            try:
                link, target = make_link(port=None if host else bridge.target,
                                         host=host, baud=args.baud,
                                         udp_port=udp_port)
            except RuntimeError:
                continue        # not back yet; the port has not returned
            if bridge.open(link, target):
                print(f"[{_label(bridge)}] reconnected on {target}")
                quiet_since.pop(index, None)

        if now - last_report < 5.0:
            continue
        last_report = now
        for index, bridge in enumerate(bridges):
            if not bridge.connected:
                continue
            print(f"[{_label(bridge)}] {bridge.sample_rate:.0f} Hz  "
                  f"{bridge.samples} samples  {bridge.flicks} flicks"
                  + (f"  {bridge.refused} refused" if bridge.refused else ""))

            # An open port that delivers nothing is its own failure, and a
            # different one from a port that will not open. Say so rather than
            # printing a reassuring 0 Hz for ever.
            if bridge.sample_rate >= 1.0:
                quiet_since.pop(index, None)
                continue
            if index not in quiet_since:
                quiet_since[index] = now
            elif now - quiet_since[index] >= 10.0:
                quiet_since[index] = now
                print("       the link is open but no samples are arriving.")
                print("       * `mode csv` may have been turned off -- the "
                      "dashboard sends `mode pretty` when it disconnects.")
                print("       * over WiFi the board streams to whoever spoke "
                      "to it last; the dashboard may have taken it over.")
                print("       See PROJECT.md for the rest.")


if __name__ == "__main__":
    sys.exit(main())
