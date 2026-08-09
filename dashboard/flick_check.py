"""Check that flicks go where they are aimed, and say how far off they are.

    python flick_check.py watch            # live: a dial that follows the board
    python flick_check.py aim              # guided: flick each lane, get a table
    python flick_check.py record run.jsonl # capture raw samples to a file
    python flick_check.py replay run.jsonl # re-run detection over a capture

Why this exists
---------------
Everything else in this project reports what the detector decided. None of it
answers the question anybody actually has, which is "when I flick up, does the
game think I flicked up". That question cannot be answered from inside the
detector, because the only thing that knows where the flick was aimed is the
person who threw it -- so the measurement has to ask them first and compare
afterwards, which is what ``aim`` does.

``aim`` separates the two kinds of wrong, and that separation is the point:

* a **constant offset** -- every flick lands the same number of degrees round
  from where it was aimed. This is a mounting angle or a wrong front axis, it
  is one number, and the game already corrects for it (``bearing_offset_deg``,
  set by the in-game direction check). It is not an accuracy problem.
* the **spread** left after taking that offset out. This is the real
  directional accuracy, it cannot be calibrated away, and it is the number to
  compare against a target like "within three degrees".

Reporting only the raw error mixes the two and makes a perfectly consistent
board with a 40 degree mounting error look hopeless, while a board with no
offset and 30 degrees of scatter looks fine.

``record`` and ``replay`` exist so that tuning is not guesswork: capture one
set of flicks, then replay it through as many settings as you like and compare
on identical data. A change measured against a fresh set of hand-thrown flicks
is measured against the hand as much as against the change.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt

from bbda.gamebridge import BridgeConfig, GameBridge, make_link
from bbda.link import Sample
from bbda.motion import AXIS_VECTORS, flick_frame, levelled_frame

#: The six lanes the game has, as bearings clockwise from straight up, with
#: names somebody can act on. These are the sector centres the bridge defaults
#: to -- 30 degree offset, so no lane sits on the 0/360 seam.
LANES = [
    (30.0, "UP-RIGHT"),
    (90.0, "RIGHT"),
    (150.0, "DOWN-RIGHT"),
    (210.0, "DOWN-LEFT"),
    (270.0, "LEFT"),
    (330.0, "UP-LEFT"),
]


def wrap180(degrees: float) -> float:
    """An angle difference folded into -180..180."""
    return (degrees + 180.0) % 360.0 - 180.0


#: Where Godot keeps the settings the game actually plays with. The front axis
#: lives here because it is a fact about how one board is mounted, and it is
#: chosen in the game's debug panel rather than on this tool's command line.
GAME_SETTINGS = (
    Path(os.environ.get("APPDATA", "~")).expanduser()
    / "Godot" / "app_userdata" / "bing bing rhythm war" / "imu_settings.cfg"
)


def read_game_front() -> tuple[str | None, str]:
    """The front axis the game is configured for, and where it was read from.

    Defaulting this tool to ``BridgeConfig``'s ``+X`` rather than to what the
    game is set to was a real mistake and worth naming, because the failure is
    silent and looks like the board's fault. Measured against the wrong front
    axis the swing plane is wrong, so the bearings are not merely offset --
    they are scrambled, differently for each direction. The run comes back with
    eighty degrees of scatter and a confident diagnosis of "mirrored", and
    every word of it is about this tool's own flag.
    """
    try:
        text = GAME_SETTINGS.read_text(encoding="utf-8")
    except OSError:
        return None, "not found"
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "front":
            axis = value.strip().strip('"').upper()
            if axis in AXIS_CHOICES:
                return axis, str(GAME_SETTINGS)
    return None, f"{GAME_SETTINGS} (no front axis in it)"


AXIS_CHOICES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------
def open_board(args) -> tuple[GameBridge, object]:
    """A bridge with its detector running over a live board.

    The real :class:`GameBridge` rather than a detector wired up by hand, so
    what this measures is what the game receives -- including the levelled
    frame, the stroke integration and the transport delay. A check that
    measured a private copy of the detector could pass while the game got
    something else entirely, which is the one failure a checking tool must not
    have.
    """
    config = BridgeConfig(
        rate_hz=args.rate,
        front=args.front,
        on_threshold_dps=args.threshold,
        verbose=False,
        # Nothing here should be able to score a note in a game that happens to
        # be running, so the flicks are never posted anywhere.
        game_port=0,
        motion_hz=0.0,
    )
    bridge = GameBridge(config)
    bridge._emit = lambda payload: None          # measure, do not broadcast
    link, target = make_link(port=args.port, host=args.host)
    if not bridge.open(link, target):
        raise SystemExit(f"could not open {target}")
    print(f"Board on {target} at {config.rate_hz} Hz, front axis {config.front}.")
    return bridge, link


def collect(bridge: GameBridge) -> list:
    """Drain and return whatever flicks the detector has produced.

    The bridge calls :meth:`_publish` for each one; this replaces it with a
    list so they can be waited for rather than watched go past.
    """
    found: list = []
    bridge._publish = lambda flick, host_now: found.append((flick, host_now))
    return found


def wait_for_flick(bridge: GameBridge, found: list, timeout: float = 12.0):
    """Block until one flick arrives, or give up. Returns it, or None."""
    del found[:]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found:
            time.sleep(0.05)        # let a doubled event settle before reading
            flick = found[0]
            del found[:]
            return flick
        time.sleep(0.01)
    return None


# ----------------------------------------------------------------------
# watch -- a dial that follows the board
# ----------------------------------------------------------------------
DIAL = 41          # characters across the compass strip; odd, so up has a centre


def dial_row(bearing: float) -> str:
    """A strip with a marker where the bearing points, 0 in the middle."""
    row = ["."] * DIAL
    for lane, _ in LANES:
        index = int(round((wrap180(lane) + 180.0) / 360.0 * (DIAL - 1)))
        row[index] = "|"
    index = int(round((wrap180(bearing) + 180.0) / 360.0 * (DIAL - 1)))
    row[index] = "#"
    return "".join(row)


def run_watch(bridge: GameBridge, found: list) -> int:
    """Print where the board is being swung, continuously.

    The fastest way to tell whether directions are right, because it needs no
    protocol and no patience: swing the board up and the marker goes to the
    middle, swing it right and the marker goes right. If it does the opposite,
    or moves along a different axis from the one being swung, that is the front
    axis being wrong and it is visible in about four seconds -- where the same
    fault, seen only through flicks landing in odd lanes, looks like bad luck
    for as long as anyone is willing to keep flicking.
    """
    print()
    print("Swing the board and watch the # move. -180 left, 0 up, +180 down.")
    print("| marks a lane centre. Ctrl-C to stop.")
    print()
    try:
        while True:
            time.sleep(1.0 / 25.0)
            swing = bridge._motion_swing
            bearing = bridge._motion_bearing
            bridge._motion_swing = 0.0
            bridge._motion_dps = 0.0
            settled = "  " if bridge.detector.gravity.trust > 0.5 else " ~"
            if swing < 15.0:
                print(f"\r  {'still':^{DIAL}}   ---- dps{settled}", end="")
            else:
                print(f"\r  {dial_row(bearing)}  {swing:5.0f} dps{settled}"
                      f"  {bearing:5.1f}deg", end="")
            if found:
                flick, _ = found[0]
                del found[:]
                print(f"\r  {dial_row(flick.bearing_deg)}  FLICK "
                      f"{flick.bearing_deg:5.1f}deg  "
                      f"{flick.rotation_deg:4.0f}deg turn  "
                      f"{flick.samples:3d} samples")
    except KeyboardInterrupt:
        print("\n")
    return 0


# ----------------------------------------------------------------------
# aim -- flick each lane, get a table
# ----------------------------------------------------------------------
def fit_offset(pairs: list[tuple[float, float]]) -> tuple[float, bool, float]:
    """The single rotation, with or without a mirror, that best explains it.

    Returns (offset degrees, mirrored, residual rms degrees). The mirror is
    tried because getting a board's handedness backwards is a real and common
    mounting mistake, and it looks exactly like scatter until it is named --
    every flick lands somewhere plausible and none of them agree.

    The offset is fitted as a circular mean rather than an arithmetic one, or
    a set of errors straddling 0 and 360 would average to 180.
    """
    best = (0.0, False, float("inf"))
    for mirrored in (False, True):
        errors = [wrap180((-aimed if mirrored else aimed) - got)
                  for aimed, got in pairs]
        offset = math.degrees(math.atan2(
            sum(math.sin(math.radians(e)) for e in errors),
            sum(math.cos(math.radians(e)) for e in errors)))
        residuals = [wrap180(e - offset) for e in errors]
        rms = math.sqrt(sum(r * r for r in residuals) / max(1, len(residuals)))
        if rms < best[2]:
            best = (offset, mirrored, rms)
    return best


def bearing_for_front(flick, front: str) -> float | None:
    """Re-read a captured flick as though this axis were the board's front.

    Possible only because the flick carries both the whole stroke as one
    rotation and the vertical at the moment it started: with those two, the
    bearing for any candidate front is a calculation rather than another
    handful of flicks. Twelve thrown flicks therefore answer "which axis is the
    front" as a by-product of answering "how accurate is it", instead of
    needing six separate runs to compare.
    """
    turn = getattr(flick, "rotation_vector", None)
    if turn is None or float(np.linalg.norm(turn)) < 1e-9:
        return None
    base = flick_frame(front)
    up = getattr(flick, "up_at_onset", None)
    frame = base if up is None else levelled_frame(
        base.front, np.asarray(up, dtype=float), base.up)
    # A movement that barely swung this candidate's front has no direction to
    # report against it -- the bearing of a near-zero vector is noise, and one
    # of those will occasionally post a very low error by luck.
    if frame.swing_fraction(turn) < 0.35:
        return None
    return float(frame.bearing_deg(turn))


def score_front(pairs: list[tuple[float, object]], front: str):
    """How well one candidate front explains a whole run: (rms, offset, flip, n)."""
    measured = []
    for lane, flick in pairs:
        bearing = bearing_for_front(flick, front)
        if bearing is not None:
            measured.append((lane, bearing))
    if len(measured) < 4:
        return (float("inf"), 0.0, False, len(measured))
    offset, flip, rms = fit_offset(measured)
    return (rms, offset, flip, len(measured))


def run_aim(bridge: GameBridge, found: list, rounds: int) -> int:
    import random

    print()
    print(f"{rounds} rounds of six. Hold the board however is comfortable --")
    print("crooked is fine and is worth testing, since that is how it is held")
    print("in play. Flick the direction named, then let it come back.")
    print()

    results: dict[float, list[float]] = {lane: [] for lane, _ in LANES}
    pairs: list[tuple[float, float]] = []
    captured: list[tuple[float, object]] = []
    order = list(LANES)
    for round_index in range(rounds):
        random.shuffle(order)      # so the hand cannot settle into a rhythm
        for lane, name in order:
            print(f"  round {round_index + 1}  flick {name:<11} "
                  f"({lane:5.1f}deg) ... ", end="", flush=True)
            got = wait_for_flick(bridge, found)
            if got is None:
                print("nothing registered -- skipped")
                continue
            flick, _ = got
            error = wrap180(lane - flick.bearing_deg)
            results[lane].append(flick.bearing_deg)
            pairs.append((lane, flick.bearing_deg))
            captured.append((lane, flick))
            print(f"went {flick.bearing_deg:6.1f}deg   off by {error:+6.1f}deg"
                  f"   {flick.rotation_deg:4.0f}deg turn over {flick.samples:3d} samples")

    if len(pairs) < 6:
        print("\nToo few flicks registered to say anything. Is the board being "
              "flicked hard enough? `watch` shows whether it sees them at all.")
        return 1

    offset, mirrored, residual_rms = fit_offset(pairs)

    print()
    print("  lane          aimed    went    off by    spread")
    print("  " + "-" * 50)
    for lane, name in LANES:
        got = results[lane]
        if not got:
            print(f"  {name:<12} {lane:6.1f}      --")
            continue
        errors = [wrap180(lane - b) for b in got]
        mean_error = statistics.fmean(errors)
        spread = statistics.pstdev(errors) if len(errors) > 1 else 0.0
        mean_bearing = wrap180(lane - mean_error) % 360.0
        print(f"  {name:<12} {lane:6.1f}  {mean_bearing:6.1f}   "
              f"{mean_error:+6.1f}    {spread:5.1f}   ({len(got)})")

    # --- which axis is the front ------------------------------------
    #
    # Asked first, and asked of the same flicks, because every other number
    # here is meaningless if it is wrong. A wrong front axis does not offset
    # the bearings, it scrambles them: the plane the board's front sweeps
    # through is the wrong plane, so each direction is wrong by a different
    # amount, and the run comes back looking like a board that cannot aim.
    in_use = bridge.config.front
    # Sorted by accuracy, then by *not* needing a mirror. An axis and its
    # opposite explain a set of planar flicks equally well -- one of them
    # reflected -- so they tie exactly, and the tie is real rather than
    # numerical. Given two explanations that fit identically, the one that does
    # not require the frame to be reflected is the right answer: a mirror is a
    # correction the game then has to carry for ever, to undo a choice made
    # here by list order.
    scored = sorted(
        ((front, *score_front(captured, front)) for front in AXIS_CHOICES),
        key=lambda row: (round(row[1], 3), row[3]))
    best_front, best_rms = scored[0][0], scored[0][1]

    print()
    print("  front axis      accuracy   flicks it could read")
    print("  " + "-" * 50)
    for front, rms, _off, flip, n in scored:
        mark = "  <- in use" if front == in_use else ""
        if front == best_front and front != in_use:
            mark = "  <- fits best"
        shown = "  --  " if rms == float("inf") else f"{rms:5.1f} deg"
        print(f"  {front:<12}   {shown}   {n:2d} of {len(captured)}"
              + ("  mirrored" if flip and rms != float("inf") else "") + mark)

    print()
    print("  What that means")
    print("  " + "-" * 50)

    # The axis in use has to be beaten convincingly, not merely edged out. With
    # a dozen hand-thrown flicks two axes can land a few degrees apart by luck,
    # and telling somebody to change a correct setting is worse than saying
    # nothing.
    in_use_rms = next(row[1] for row in scored if row[0] == in_use)
    if best_front != in_use and best_rms < in_use_rms * 0.6:
        print(f"  * THE FRONT AXIS IS WRONG. These flicks were read as though")
        print(f"    {in_use} pointed away from you, which leaves {in_use_rms:.0f} degrees of")
        print(f"    scatter. Read as {best_front} the same flicks come out to "
              f"{best_rms:.1f} degrees.")
        print(f"    Nothing else below is worth acting on until that is fixed.")
        print()
        print(f"    Set it in the game's debug panel (Orientation), or re-run")
        print(f"    this with --front {best_front}. The bridge takes it as")
        print(f"    --front {best_front} too.")
        print()
        offset, mirrored, residual_rms = (scored[0][2], scored[0][3], best_rms)
        print(f"    Read as {best_front}, the rest of this run says:")
    elif mirrored:
        print("  * The board reads MIRRORED -- clockwise and anticlockwise are")
        print("    swapped. That is a wrong front axis, not a tuning problem.")
        print("    Run the direction check in the game's debug panel, or try")
        print(f"    --front with the opposite sign of {in_use}.")
    if abs(offset) > 5.0:
        print(f"  * Everything is rotated by {offset:+.1f} degrees. This is one")
        print("    number and the game already corrects it -- run the direction")
        print("    check in the debug panel and it will be applied for you.")
        print("    It is not an accuracy problem.")
    else:
        print(f"  * No meaningful constant offset ({offset:+.1f} degrees).")
    print()
    print(f"  * Accuracy, once that offset is taken out: {residual_rms:.1f} degrees rms.")
    if residual_rms <= 3.0:
        print("    Inside three degrees. The sensor is not what is limiting")
        print("    this any more -- the hand is.")
    elif residual_rms <= 10.0:
        print("    Good enough to play: lanes are 60 degrees apart, so this is")
        print("    comfortably inside one. Getting below three needs throwing")
        print("    flicks more consistently, not tuning.")
    else:
        print("    Wide. Lanes are 60 degrees apart, so this will put flicks in")
        print("    the wrong lane. Check with `watch` that the arrow follows the")
        print("    board sensibly, and that the accelerometer reads 1 g at rest")
        print("    -- directions are measured against gravity, and a board whose")
        print("    calibration makes it read 3 g cannot find vertical at all.")

    worst = max((abs(wrap180(lane - b)) for lane, b in pairs))
    print()
    print(f"  * Worst single flick: {worst:.1f} degrees from where it was aimed.")
    print(f"  * Transport delay while measuring: {bridge.peak_transport_ms:.1f} ms "
          f"worst, {bridge.last_transport_ms:.1f} ms last.")
    return 0


# ----------------------------------------------------------------------
# record / replay
# ----------------------------------------------------------------------
def run_record(bridge: GameBridge, link, path: str, seconds: float) -> int:
    """Write raw samples to a file, so tuning can be compared on one dataset."""
    captured: list[dict] = []
    original = bridge._on_sample

    def tap(sample):
        captured.append({
            "t": float(sample.t),
            "host_t": float(getattr(sample, "host_t", 0.0) or 0.0),
            "accel": [float(v) for v in sample.accel],
            "gyro": [float(v) for v in sample.gyro],
        })
        original(sample)

    bridge._on_sample = tap
    link.sample.disconnect()
    link.sample.connect(tap, Qt.ConnectionType.DirectConnection)

    print(f"\nRecording {seconds:.0f}s to {path}. Flick away -- and say out loud "
          f"or write down\nwhich way each one went, because the file cannot "
          f"know.\n")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.2)
        print(f"\r  {len(captured):6d} samples, {end - time.monotonic():4.0f}s left",
              end="")
    with open(path, "w", encoding="utf-8") as handle:
        for row in captured:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"\n\n  {len(captured)} samples written to {path}")
    print(f"  Replay with:  python flick_check.py replay {path}")
    return 0


def run_replay(path: str, args) -> int:
    """Re-run detection over a capture, with whatever tuning is asked for."""
    config = BridgeConfig(
        front=args.front,
        on_threshold_dps=args.threshold,
        commit_fraction=args.commit,
        level_with_gravity=not args.no_level,
        verbose=False, game_port=0, motion_hz=0.0,
    )
    bridge = GameBridge(config)
    bridge._emit = lambda payload: None
    found = collect(bridge)

    rows = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows += 1
            bridge._on_sample(Sample(
                t=row["t"],
                accel=np.array(row["accel"], dtype=float),
                gyro=np.array(row["gyro"], dtype=float),
                mag=np.zeros(3), temp=25.0, mag_fresh=False,
                host_t=row.get("host_t", 0.0),
            ))

    print(f"\n{rows} samples, front {config.front}, threshold "
          f"{config.on_threshold_dps:.0f} dps, commit {config.commit_fraction:.2f}, "
          f"levelling {'off' if args.no_level else 'on'}")
    print(f"{len(found)} flicks\n")
    print("     t       bearing   turn  samples  peak dps  dur ms  detect ms")
    print("  " + "-" * 66)
    for flick, _ in found:
        detect_ms = (flick.t - flick.peak_t) * 1000.0
        print(f"  {flick.t:8.3f}  {flick.bearing_deg:7.1f}  {flick.rotation_deg:5.0f}"
              f"  {flick.samples:6d}  {flick.peak_dps:8.0f}  {flick.duration_ms:6.0f}"
              f"  {detect_ms:8.1f}")
    if bridge.refused:
        print(f"\n  {bridge.refused} refused; last: {bridge.last_refusal_text}")
    return 0


# ----------------------------------------------------------------------
def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="Check that flicks go where they are aimed.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("mode",
                        choices=["watch", "aim", "record", "replay"])
    parser.add_argument("file", nargs="?", help="capture file, for record/replay")
    parser.add_argument("--port", help="serial port (default: find the board)")
    parser.add_argument("--host", help="board IP, to use WiFi instead")
    parser.add_argument("--rate", type=int, default=400,
                        help="samples per second to ask for (default: %(default)s)")
    # No default here. It is filled in below from what the game is actually
    # set to, and only falls back to the stock axis if that cannot be read --
    # a checking tool measuring against a different front axis from the one
    # being played on is measuring its own flag.
    parser.add_argument("--front", default=None,
                        choices=["+X", "-X", "+Y", "-Y", "+Z", "-Z"],
                        help="board axis pointing away from you "
                             "(default: whatever the game is set to)")
    parser.add_argument("--threshold", type=float,
                        default=BridgeConfig().on_threshold_dps)
    parser.add_argument("--commit", type=float,
                        default=BridgeConfig().commit_fraction,
                        help="replay only: fraction of peak that ends a stroke")
    parser.add_argument("--no-level", action="store_true",
                        help="replay only: measure against the board's own axes "
                             "rather than against gravity")
    parser.add_argument("--rounds", type=int, default=2,
                        help="aim only: passes through all six lanes")
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="record only: how long to capture")
    args = parser.parse_args()

    if args.front is None:
        args.front, where = read_game_front()
        if args.front is None:
            args.front = BridgeConfig().front
            print(f"note: could not read the game's front axis ({where}), so "
                  f"this is measuring against {args.front}. If that is not how "
                  f"the board is mounted, pass --front.")
        else:
            print(f"Front axis {args.front}, from the game's own settings.")

    if args.mode == "replay":
        if not args.file:
            parser.error("replay needs a file")
        return run_replay(args.file, args)

    try:
        bridge, link = open_board(args)
    except (RuntimeError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    found = collect(bridge)
    try:
        if args.mode == "watch":
            return run_watch(bridge, found)
        if args.mode == "aim":
            return run_aim(bridge, found, args.rounds)
        if not args.file:
            parser.error("record needs a file")
        return run_record(bridge, link, args.file, args.seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 1
    finally:
        bridge.close()


if __name__ == "__main__":
    sys.exit(main())
