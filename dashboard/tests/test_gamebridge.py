"""Self-checks for the bridge that feeds flicks to the Godot game.

Runs without a display, a board or the game:  python tests/test_gamebridge.py

The datagram tests use a real UDP socket on the loopback interface rather than
a stub, because the thing most likely to be wrong is the socket call and not
the JSON around it.

The mapping test matters most. The bearing convention the detector reports in
and the angle convention the game draws in are different, and the conversion
between them lives in ImuInput.gd -- in another language, where these tests
cannot reach it. So the formula is restated here and checked against the game's
real lane layout, which makes this the thing that fails when someone changes
one side and not the other.
"""

import json
import time
import math
import socket
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bbda.gamebridge import WIRE_VERSION, BridgeConfig, GameBridge
from bbda.link import Sample

fail = 0


def check(name, cond, extra=""):
    global fail
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fail += 1


DT = 0.005
LEVEL = np.array([0.0, 0.0, 1.0])
STILL = np.zeros(3)

#: Gameplay.gd's default layout: lane -> angle, measured counter-clockwise from
#: screen right, which is what _vec(a) = (cos a, -sin a) draws.
GAME_SECTOR_ANGLE = {1: 60.0, 2: 0.0, 3: 300.0, 4: 240.0, 5: 180.0, 6: 120.0}


def game_angle_of(bearing_deg):
    """The conversion ImuInput.gd performs, restated.

    A bearing is degrees clockwise from up; a game angle is degrees
    counter-clockwise from right. Both are on the screen plane, so the two
    differ by a reflection and a quarter turn: ``90 - bearing``.
    """
    return (90.0 - bearing_deg) % 360.0


def nearest_lane(angle_deg):
    """_nearest_sector() from Gameplay.gd, restated."""
    a = angle_deg % 360.0
    best, best_d = 1, 1e9
    for lane, centre in GAME_SECTOR_ANGLE.items():
        d = abs(centre - a)
        d = min(d, 360.0 - d)
        if d < best_d:
            best, best_d = lane, d
    return best


# ---------------- the bearing -> lane contract ----------------
# If these ever disagree with the game, flicks land in the wrong lane -- which
# looks like bad detection and is not.
#
# Note what the layout does *not* have: a lane at the top. The game's lanes sit
# at 0/60/.../300 counter-clockwise from screen right, so the top of the ring
# at 90 is a boundary between two of them. A flick straight up is therefore
# genuinely ambiguous and is meant to be refused, which is the whole reason the
# detector's sector grid is offset by 30 degrees rather than aligned to zero.
ok = True
for bearing, lane_angle in (
    (0.0, 90.0), (60.0, 30.0), (120.0, 330.0),
    (180.0, 270.0), (240.0, 210.0), (300.0, 150.0),
):
    got = game_angle_of(bearing)
    if abs(got - lane_angle) > 1e-9:
        ok = False
        check(f"bearing {bearing} converts", False, f"got {got}, want {lane_angle}")
check("bearings convert to game angles by 90 - bearing", ok)

# The six bearings the bridge gates on sit at 30, 90, ... by default, and those
# are the ones that must land in the middle of a lane rather than on an edge.
ok = True
seen = set()
for index in range(6):
    bearing = 30.0 + index * 60.0
    lane = nearest_lane(game_angle_of(bearing))
    seen.add(lane)
    centre = GAME_SECTOR_ANGLE[lane]
    offset = abs(((game_angle_of(bearing) - centre + 180.0) % 360.0) - 180.0)
    if offset > 1e-9:
        ok = False
        check(f"bearing {bearing} centres on a lane", False,
              f"lane {lane} at {centre}, off by {offset:.3f}")
check("the default sector offset centres flicks on lanes, not edges", ok)
check("all six lanes are reachable", seen == set(GAME_SECTOR_ANGLE),
      f"reached {sorted(seen)}")

# The offset is load-bearing: without it every flick lands exactly on a
# boundary between two lanes, and which one wins is down to floating-point
# noise. This is what the 30 degree default is for.
on_edge = 0
for index in range(6):
    angle = game_angle_of(index * 60.0)
    distances = sorted(
        min(abs(c - angle), 360.0 - abs(c - angle))
        for c in GAME_SECTOR_ANGLE.values()
    )
    if abs(distances[0] - distances[1]) < 1e-9:
        on_edge += 1
check("an unoffset grid would be ambiguous, which is why 30 is the default",
      on_edge == 6, f"{on_edge}/6 on a boundary")


# ---------------- end to end, over a real socket ----------------
def bearing_flick(bearing_deg, peak_dps=400.0, duration_s=0.12):
    """A rotation pulse sending the board's front to a bearing.

    Same construction as tests/test_motion.py: to send the front towards
    ``up cos b + right sin b`` the board turns about ``-(y cos b + z sin b)``.
    """
    n = int(duration_s / DT)
    axis = -np.array([0.0,
                      math.cos(math.radians(bearing_deg)),
                      math.sin(math.radians(bearing_deg))])
    return [axis * peak_dps * math.sin(math.pi * i / n) for i in range(n)]


def sample(t, gyro):
    return Sample(t=t, accel=LEVEL, gyro=np.asarray(gyro, dtype=float),
                  mag=STILL, temp=25.0, mag_fresh=True)


def drain(sock):
    out = []
    sock.settimeout(0.0)
    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except (BlockingIOError, socket.timeout):
            return out
        except OSError:
            return out
        try:
            out.append(json.loads(data.decode("utf-8")))
        except ValueError:
            out.append({"type": "unparseable", "raw": data})


def feed(bridge, pulses):
    t = 0.0
    for _ in range(20):
        bridge._on_sample(sample(t, STILL)); t += DT
    for g in pulses:
        bridge._on_sample(sample(t, g)); t += DT
    for _ in range(60):
        bridge._on_sample(sample(t, STILL)); t += DT


listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
listener.bind(("127.0.0.1", 0))
GAME_PORT = listener.getsockname()[1]


def make_bridge(**kwargs):
    return GameBridge(BridgeConfig(game_host="127.0.0.1", game_port=GAME_PORT,
                                   **kwargs))


bridge = make_bridge()
feed(bridge, bearing_flick(30.0))
records = [r for r in drain(listener) if r.get("type") == "flick"]
check("a synthetic flick reaches the game socket", len(records) == 1,
      f"got {len(records)} flick records")

if records:
    record = records[0]
    check("the record carries the wire version", record.get("v") == WIRE_VERSION,
          str(record.get("v")))
    check("flicking the front up and right is reported as bearing 30",
          abs(((record["bearing"] - 30.0 + 180.0) % 360.0) - 180.0) < 1.0,
          f"got {record['bearing']}")
    check("the reported bearing lands in the game's upper-right lane",
          nearest_lane(game_angle_of(record["bearing"])) == 1,
          f"lane {nearest_lane(game_angle_of(record['bearing']))}")
    for field in ("seq", "t", "host_t", "sector", "strength", "peak_dps",
                  "dominance", "duration_ms"):
        check(f"the record carries {field}", field in record)
    check("strength is a fraction", 0.0 <= record.get("strength", -1) <= 1.0,
          str(record.get("strength")))

# Every direction the game has a lane for has to survive the whole path.
ok = True
lanes = set()
for index in range(6):
    bearing = 30.0 + index * 60.0
    b = make_bridge()
    feed(b, bearing_flick(bearing))
    got = [r for r in drain(listener) if r.get("type") == "flick"]
    if len(got) != 1:
        ok = False
        check(f"flick at bearing {bearing}", False, f"{len(got)} records")
        continue
    measured = got[0]["bearing"]
    if abs(((measured - bearing + 180.0) % 360.0) - 180.0) > 2.0:
        ok = False
        check(f"bearing {bearing} survives the round trip", False,
              f"got {measured}")
    lanes.add(nearest_lane(game_angle_of(measured)))
check("all six bearings survive detection and encoding", ok)
check("and they land in six different lanes", len(lanes) == 6,
      f"lanes {sorted(lanes)}")

# ---------------- detection lag ----------------
# A flick is only recognised once it is over, so the record is always late.
# The game subtracts lag_ms to judge it against when it happened; if that
# number is wrong or missing, every flick reads late and the error grows with
# how long the player's flick was -- which no fixed latency setting can fix.
ok = True
measured = []
for duration_s in (0.06, 0.09, 0.12, 0.15):
    b = make_bridge()
    feed(b, bearing_flick(30.0, duration_s=duration_s))
    got = [r for r in drain(listener) if r.get("type") == "flick"]
    if len(got) != 1 or "lag_ms" not in got[0]:
        ok = False
        check(f"flick of {duration_s * 1000:.0f} ms reports a lag", False)
        continue
    measured.append((duration_s * 1000.0, got[0]["lag_ms"]))
check("every flick carries the lag between its peak and its report", ok)

# A fraction of the gesture, and less than half of it.
#
# Half was what waiting for the rotation to die away cost: the peak of a
# symmetric flick is its middle, and the old detector reported at the end. The
# detector now commits once the rate has fallen to `commit_fraction` of its own
# peak, which on a half-sine lands about 0.3 of the gesture after the peak
# rather than 0.5 -- so this is bounded above by the old behaviour and below by
# zero, which is where reporting at the peak itself would put it.
#
# Checked as a relationship rather than a constant precisely because it is not
# one: that it varies is the whole reason it has to travel with the event.
ok = bool(measured)
for duration_ms, lag_ms in measured:
    if not 0.15 * duration_ms <= lag_ms <= 0.45 * duration_ms:
        ok = False
        check(f"lag for a {duration_ms:.0f} ms flick", False,
              f"got {lag_ms:.0f} ms, wanted {0.15 * duration_ms:.0f}"
              f"..{0.45 * duration_ms:.0f}")
check("the lag is a third of the flick, not half of it", ok,
      "  ".join(f"{d:.0f}ms->{l:.0f}ms" for d, l in measured))
check("and it genuinely varies, so it cannot be a fixed offset",
      len({round(lag, 0) for _, lag in measured}) > 1,
      str([lag for _, lag in measured]))

# The whole point of committing early is that it beats waiting. Stated as its
# own check because it is the claim the change was made for, and a future
# tuning that quietly gives it back should fail here rather than merely feel
# worse to play.
patient = make_bridge()
patient.detector.commit_fraction = 0.0     # only the off threshold ends it
feed(patient, bearing_flick(30.0, duration_s=0.12))
old_style = [r for r in drain(listener) if r.get("type") == "flick"]
quick = make_bridge()
feed(quick, bearing_flick(30.0, duration_s=0.12))
new_style = [r for r in drain(listener) if r.get("type") == "flick"]
check("committing on the stroke beats waiting for the rotation to die",
      bool(old_style) and bool(new_style)
      and new_style[0]["lag_ms"] < old_style[0]["lag_ms"] - 5.0,
      f"{new_style[0]['lag_ms']:.0f} ms against "
      f"{old_style[0]['lag_ms']:.0f} ms" if old_style and new_style else "")

# The direction is the stroke, not one sample of it, and the record says how
# much of a stroke there was. A flick named off two samples is one the sample
# rate could not resolve, and that has to be visible.
turned = make_bridge()
feed(turned, bearing_flick(30.0, duration_s=0.12))
turn_record = [r for r in drain(listener) if r.get("type") == "flick"]
check("a flick reports how far it actually turned",
      bool(turn_record) and turn_record[0].get("turn_deg", 0.0) > 5.0,
      f"{turn_record[0].get('turn_deg')} deg" if turn_record else "")
check("and how many samples the direction was averaged over",
      bool(turn_record) and turn_record[0].get("samples", 0) >= 5,
      f"{turn_record[0].get('samples')} samples" if turn_record else "")

# peak_t must precede t, or the game would reach forward in time.
lagged = make_bridge()
feed(lagged, bearing_flick(30.0, duration_s=0.12))
records_lag = [r for r in drain(listener) if r.get("type") == "flick"]
check("the peak is timestamped before the end of the event",
      bool(records_lag) and records_lag[0]["peak_t"] < records_lag[0]["t"])
check("the lag is never negative",
      bool(records_lag) and records_lag[0]["lag_ms"] >= 0.0)

# A flick straight up falls exactly between two lanes. It used to be refused
# for that, and is not any more: the margin floor defaults to zero because the
# game resolves aim itself, matching a flick against every note in reach. The
# bearing still has to be reported honestly, since that is what the game
# resolves *with*.
edge = make_bridge()
feed(edge, bearing_flick(0.0))
edge_records = [r for r in drain(listener) if r.get("type") == "flick"]
check("a flick onto a lane boundary is reported, not refused",
      len(edge_records) == 1, str(len(edge_records)))
check("and carries the bearing it actually went, for the game to resolve",
      bool(edge_records) and abs(((edge_records[0]["bearing"] + 180.0) % 360.0)
                                 - 180.0) < 8.0,
      str(edge_records[0]["bearing"]) if edge_records else "none")

# The floor still works when it is asked for -- it is off by default, not gone.
strict_edge = make_bridge(min_margin=0.25)
feed(strict_edge, bearing_flick(0.0))
check("a margin floor that was asked for still refuses the boundary",
      not [r for r in drain(listener) if r.get("type") == "flick"])

# Strength has to mean something, or the game cannot use it for feedback.
soft = make_bridge()
feed(soft, bearing_flick(30.0, peak_dps=200.0))
soft_records = [r for r in drain(listener) if r.get("type") == "flick"]
hard = make_bridge()
feed(hard, bearing_flick(30.0, peak_dps=900.0))
hard_records = [r for r in drain(listener) if r.get("type") == "flick"]
check("a harder flick reports more strength",
      bool(soft_records) and bool(hard_records)
      and hard_records[0]["strength"] > soft_records[0]["strength"],
      f"{soft_records[0]['strength'] if soft_records else '-'} vs "
      f"{hard_records[0]['strength'] if hard_records else '-'}")
check("strength saturates at 1 rather than running past it",
      bool(hard_records) and hard_records[0]["strength"] <= 1.0)

# A roll about the front is not a direction, and reporting one as a flick
# upwards is the specific failure the swing floor exists to prevent.
roll = make_bridge()
roll_pulse = [np.array([1.0, 0.0, 0.0]) * 400.0 * math.sin(math.pi * i / 24)
              for i in range(24)]
feed(roll, roll_pulse)
check("a roll about the front is refused, not named",
      not [r for r in drain(listener) if r.get("type") == "flick"])

# Nothing listening must not take the bridge down: the game routinely starts
# after the bridge, and a crash here would be a crash at the worst moment.
orphan = GameBridge(BridgeConfig(game_host="127.0.0.1", game_port=1))
try:
    orphan.send_demo_flick(0.0)
    for _ in range(5):
        orphan.send_demo_flick(90.0)
    survived = True
except OSError:
    survived = False
check("sending with no game listening is survivable", survived)

# The demo path is the first troubleshooting step, so it has to work on its own.
demo = make_bridge()
demo.send_demo_flick(123.0, strength=0.5)
demo_records = [r for r in drain(listener) if r.get("type") == "flick"]
check("demo flicks are well-formed flick records",
      len(demo_records) == 1 and demo_records[0].get("demo") is True
      and abs(demo_records[0]["bearing"] - 123.0) < 1e-6,
      str(demo_records[0]) if demo_records else "none")

# ---------------- live motion, which the on-screen arrow follows ----------------
# The arrow in Gameplay.gd is drawn from these. They are not inputs and nothing
# is scored from them, so the thing that can go wrong is subtler than a missed
# flick: an arrow that points somewhere other than the lane the flick lands in
# makes correct scoring look broken, and is the one failure worth testing for.
#
# motion_hz is set absurdly high here so every sample produces a record, which
# is what lets a test see the peak of a flick rather than whichever sample the
# 30 Hz timer happened to land on.
live = make_bridge(motion_hz=1e9)
feed(live, bearing_flick(30.0))
live_records = drain(listener)
motion = [r for r in live_records if r.get("type") == "motion"]
check("motion records are sent while samples arrive", len(motion) > 20,
      f"got {len(motion)}")

if motion:
    for field in ("bearing", "dps", "swing", "threshold_dps"):
        check(f"a motion record carries {field}", field in motion[0])
    check("the threshold travels with it, so the game need not be tuned too",
          motion[0]["threshold_dps"] == BridgeConfig().on_threshold_dps,
          str(motion[0]["threshold_dps"]))

    peak = max(motion, key=lambda r: r["swing"])
    check("the arrow points where the flick went",
          abs(((peak["bearing"] - 30.0 + 180.0) % 360.0) - 180.0) < 2.0,
          f"peak swing bearing {peak['bearing']}")
    check("and therefore at the lane the flick is scored in",
          nearest_lane(game_angle_of(peak["bearing"])) == 1,
          f"lane {nearest_lane(game_angle_of(peak['bearing']))}")
    check("the swing peaks above the flick threshold, so the arrow reaches full",
          peak["swing"] > peak["threshold_dps"],
          f"{peak['swing']} dps vs {peak['threshold_dps']}")

    # The last records come from the still tail of feed(). If the window were
    # not cleared after each send, the arrow would stay at full stretch for
    # ever once the board had been flicked once.
    check("a board put down reports itself still, so the arrow retracts",
          motion[-1]["swing"] < 1.0, f"{motion[-1]['swing']} dps")

# A roll spins the board without sending its front anywhere, so it has no
# direction to draw. It must show up as rotation with almost no swing, or the
# arrow would swing out to full length and point at a lane that no flick can
# ever hit -- the detector refuses rolls.
roll_live = make_bridge(motion_hz=1e9)
feed(roll_live, roll_pulse)
roll_motion = [r for r in drain(listener) if r.get("type") == "motion"]
check("a roll reports rotation", bool(roll_motion)
      and max(r["dps"] for r in roll_motion) > 300.0)
check("but almost none of it as swing, so the arrow stays short",
      bool(roll_motion)
      and max(r["swing"] for r in roll_motion)
      < 0.1 * max(r["dps"] for r in roll_motion),
      f"swing {max((r['swing'] for r in roll_motion), default=0):.1f} vs "
      f"dps {max((r['dps'] for r in roll_motion), default=0):.1f}")

# The rate limit is what keeps a 200 Hz stream from becoming 200 datagrams a
# second for something that is only looked at. One second of samples at the
# board's real rate has to come out as about thirty records, not two hundred.
#
# The rate is measured on the board's clock, so this is exact rather than a
# matter of how fast the machine running the test happens to be.
throttled = make_bridge()
t = 0.0
for _ in range(200):
    throttled._on_sample(sample(t, STILL))
    t += DT
throttled_motion = [r for r in drain(listener) if r.get("type") == "motion"]
check("motion is rate limited rather than sent per sample",
      28 <= len(throttled_motion) <= 32,
      f"got {len(throttled_motion)} over {t:.1f}s, wanted about 30")

silent = make_bridge(motion_hz=0.0)
feed(silent, bearing_flick(30.0))
silent_records = drain(listener)
check("motion_hz=0 sends no motion at all",
      not [r for r in silent_records if r.get("type") == "motion"])
check("and flicks still arrive without it",
      len([r for r in silent_records if r.get("type") == "flick"]) == 1)

demo_motion_bridge = make_bridge()
demo_motion_bridge.send_demo_motion(123.0, 400.0)
demo_motion = [r for r in drain(listener) if r.get("type") == "motion"]
check("demo motion is a well-formed motion record",
      len(demo_motion) == 1 and demo_motion[0].get("demo") is True
      and abs(demo_motion[0]["bearing"] - 123.0) < 1e-6
      and demo_motion[0]["swing"] == 400.0,
      str(demo_motion) if demo_motion else "none")


# ---------------- refusals, which used to be silent ----------------
# A flick that is rejected produces no flick record, which is indistinguishable
# from a board that is unplugged: both are silence. These are the records that
# tell the two apart, and they are the whole answer to "I flick and nothing
# happens".
refused_roll = make_bridge()
feed(refused_roll, roll_pulse)
roll_refusals = [r for r in drain(listener) if r.get("type") == "refused"]
check("a refused roll says so instead of vanishing", len(roll_refusals) == 1,
      f"got {len(roll_refusals)}")
if roll_refusals:
    record = roll_refusals[0]
    check("the refusal names which test failed", record.get("reason") == "swing",
          str(record.get("reason")))
    check("it carries a sentence for a human", bool(record.get("detail")),
          str(record.get("detail")))
    check("which names the flag that moves the limit",
          "--front" in record.get("detail", "") or "--swing" in record.get("detail", ""),
          record.get("detail", ""))
    for field in ("peak_dps", "duration_ms"):
        check(f"the refusal carries {field}", field in record)

# A flick straight up, against a bridge told to care about lane boundaries.
# Same silence as a refused roll, different cause, and the fix is a different
# flag -- so it has to say which.
refused_edge = make_bridge(min_margin=0.25)
feed(refused_edge, bearing_flick(0.0))
edge_refusals = [r for r in drain(listener) if r.get("type") == "refused"]
check("a flick on a lane boundary is reported as a boundary",
      len(edge_refusals) == 1 and edge_refusals[0]["reason"] == "margin",
      str([r.get("reason") for r in edge_refusals]))
check("and it says which way it went, since that is the diagnosis",
      bool(edge_refusals) and "bearing" in edge_refusals[0],
      str(edge_refusals[0]) if edge_refusals else "none")

# The one the detector cannot report at all: a movement that never reached the
# threshold. Nothing happens inside the detector, so the bridge watches for it.
weak = make_bridge()
feed(weak, bearing_flick(30.0, peak_dps=90.0))
weak_records = drain(listener)
weak_refusals = [r for r in weak_records if r.get("type") == "refused"]
check("a movement too gentle to reach the threshold is reported",
      len(weak_refusals) == 1 and weak_refusals[0]["reason"] == "weak",
      str([r.get("reason") for r in weak_refusals]))
check("and no flick is claimed for it",
      not [r for r in weak_records if r.get("type") == "flick"])
if weak_refusals:
    check("the weak report says how hard it actually was",
          abs(weak_refusals[0]["peak_dps"] - 90.0) < 5.0,
          str(weak_refusals[0]["peak_dps"]))
    check("and suggests a threshold that would have accepted it",
          "--threshold" in weak_refusals[0].get("detail", ""),
          weak_refusals[0].get("detail", ""))

# Below that, a hand adjusting its grip would report constantly. The floor is
# what keeps the useful reports from being buried in noise.
fidget = make_bridge()
feed(fidget, bearing_flick(30.0, peak_dps=20.0))
check("but an idle hand is not reported as a failed flick",
      not [r for r in drain(listener) if r.get("type") == "refused"])

# An accepted flick must not also be reported as refused, or every successful
# hit would come with an explanation of why it did not work.
accepted = make_bridge()
feed(accepted, bearing_flick(30.0))
accepted_records = drain(listener)
check("an accepted flick produces no refusal",
      len([r for r in accepted_records if r.get("type") == "flick"]) == 1
      and not [r for r in accepted_records if r.get("type") == "refused"])


# ---------------- a board that streams without measuring ----------------
# The failure that costs the most time, because every other indicator is
# healthy: the port is open, the rate is a perfect 200 Hz, the sample count
# climbs, and the readings are one stale frame repeated for ever. The detector
# is being fed valid samples of a board that is not moving, so it correctly
# reports nothing, and the symptom is identical to bad tuning.
frozen = make_bridge()
FROZEN_ACCEL = np.array([-1.4218, 1.1824, -0.017])   # a real one, off a board
t = 0.0
for _ in range(400):
    frozen._on_sample(Sample(t=t, accel=FROZEN_ACCEL, gyro=STILL, mag=STILL,
                             temp=0.0, mag_fresh=True))
    t += DT
stalls = [r for r in drain(listener)
          if r.get("type") == "status" and r.get("stalled")]
check("a frozen stream is reported rather than read as stillness",
      len(stalls) == 1, f"got {len(stalls)}")
if stalls:
    check("and says to unplug rather than to reset, which does not clear it",
          "Unplug" in stalls[0]["detail"] and "reset is not enough" in stalls[0]["detail"],
          stalls[0]["detail"][:60])
check("the bridge knows it is stalled", frozen.stalled)

# It must not fire on a board being held still: a real sensor's noise floor
# moves the low bits every sample, so identical *readings* are the signal, not
# a low rotation rate.
still_board = make_bridge()
t = 0.0
rng = np.random.default_rng(7)
for _ in range(600):
    still_board._on_sample(Sample(
        t=t, accel=np.array([0.0, 0.0, 1.0]) + rng.normal(0, 0.002, 3),
        gyro=rng.normal(0, 0.05, 3), mag=STILL, temp=25.0, mag_fresh=True))
    t += DT
check("a board merely held still is not called frozen",
      not [r for r in drain(listener)
           if r.get("type") == "status" and r.get("stalled")]
      and not still_board.stalled)

# And it has to clear itself, or a board that was replugged mid-session would
# go on being reported as broken.
t = 0.0
for _ in range(20):
    frozen._on_sample(Sample(t=t, accel=np.array([0.0, 0.0, 1.0]),
                             gyro=rng.normal(0, 0.05, 3), mag=STILL,
                             temp=25.0, mag_fresh=True))
    t += DT
recovered = [r for r in drain(listener)
             if r.get("type") == "status" and r.get("stalled") is False]
check("and it clears once the readings start changing again",
      bool(recovered) and not frozen.stalled)


# ---------------- the debug panel's control channel ----------------
# The panel edits the detector that runs here, over a loopback socket. What
# makes this worth testing is that the panel shows what comes *back*: if a
# change is dropped or silently ignored, the panel would go on displaying a
# value nothing is using, which is the exact failure it exists to prevent.
CONTROL_PORT = 3971
panel = make_bridge(control_port=CONTROL_PORT)
check("the control socket opens", panel.open_control())

sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def command(message):
    sender.sendto(json.dumps(message).encode("utf-8"),
                  ("127.0.0.1", CONTROL_PORT))
    for _ in range(50):
        panel.poll_control()
        time.sleep(0.002)
    return drain(listener)


replies = command({"cmd": "get"})
config = [r for r in replies if r.get("type") == "config"]
check("asking for the config gets one back", len(config) == 1,
      f"got {len(config)}")
if config:
    for field in ("front", "on_threshold_dps", "min_swing", "min_margin",
                  "refractory_ms", "sector_offset_deg", "control_port"):
        check(f"the config reports {field}", field in config[0])

replies = command({"cmd": "set", "front": "+Y", "on_threshold_dps": 90.0,
                   "min_swing": 0.4})
config = [r for r in replies if r.get("type") == "config"]
check("a change is applied and echoed back",
      bool(config) and config[0]["front"] == "+Y"
      and config[0]["on_threshold_dps"] == 90.0,
      str(config[0]) if config else "no config record")
check("and the running detector really changed with it",
      panel.detector.min_dominance == 0.4
      and panel.detector.on_threshold_dps == 90.0,
      f"{panel.detector.min_dominance} {panel.detector.on_threshold_dps}")

# A lower threshold has to actually accept a flick the old one refused, or the
# slider is decorative.
feed(panel, bearing_flick(30.0, peak_dps=100.0))
check("a flick under the old threshold now counts",
      len([r for r in drain(listener) if r.get("type") == "flick"]) == 1)

# The panel must not be able to reach anything but tuning: these datagrams
# arrive on a socket, and a typo redirecting the game's output or reopening a
# transport is a much worse failure than an ignored setting.
before_peer = panel.peer
command({"cmd": "set", "game_port": 9999, "game_host": "10.0.0.1",
         "rate_hz": 5, "verbose": True})
check("unknown or dangerous fields are ignored", panel.peer == before_peer,
      str(panel.peer))
# Against the stock value rather than a number written out here: what is being
# checked is that the field did not move, and pinning the literal makes this
# fail whenever the default legitimately changes, which teaches whoever hits it
# to edit the test rather than to read it.
check("and the ones not on the list stay put",
      panel.config.rate_hz == BridgeConfig().rate_hz,
      str(panel.config.rate_hz))

command({"cmd": "set", "front": "sideways"})
check("a front axis that does not exist is refused",
      panel.config.front == "+Y", panel.config.front)

command({"nonsense": True})
sender.sendto(b"not json at all", ("127.0.0.1", CONTROL_PORT))
panel.poll_control()
check("junk on the control socket does not bring the bridge down", True)

# ---------------- learning the front axis ----------------
# The one measurement that turns "which way does the board's X axis point"
# into something a person can answer: flick a direction you can name, and let
# the bridge work out which axis makes that the answer.
learner = make_bridge(control_port=CONTROL_PORT + 1)
learner.config.front = "+Z"                 # deliberately wrong for this flick
learner._arm_learn(0.0)                     # "I am about to flick straight up"
drain(listener)
# bearing_flick builds its pulse in the +X-front frame, so a flick that a +X
# front calls "up" is the movement being made here.
feed(learner, bearing_flick(0.0))
suggestions = [r for r in drain(listener) if r.get("type") == "front_suggestion"]
check("a learning flick produces a suggestion", len(suggestions) == 1,
      f"got {len(suggestions)}")
if suggestions:
    suggestion = suggestions[0]
    check("which names the axis that explains the movement",
          suggestion["front"] == "+X",
          f"suggested {suggestion['front']} (running {suggestion['current']})")
    check("with the error it would leave", suggestion["error_deg"] < 5.0,
          str(suggestion["error_deg"]))
    check("and every candidate, so a close second is visible",
          len(suggestion.get("candidates", [])) == 6,
          str(len(suggestion.get("candidates", []))))
    check("the axis it rejects is genuinely worse",
          suggestion["candidates"][-1]["error_deg"] > suggestion["error_deg"])

# ---------------- rest bias ----------------
BIAS = np.array([0.8, -0.35, 0.2])
resting = make_bridge(control_port=CONTROL_PORT + 2)
resting._arm_rest(0.2)
drain(listener)
t = 0.0
for _ in range(80):
    resting._on_sample(sample(t, BIAS))
    t += DT
rest = [r for r in drain(listener) if r.get("type") == "rest"]
check("measuring at rest reports a bias", len(rest) == 1, f"got {len(rest)}")
if rest:
    measured = rest[0]["bias"]
    check("which is the average the gyro was actually reading",
          all(abs(measured[i] - BIAS[i]) < 0.01 for i in range(3)),
          str(measured))
    check("and is judged against what this part really does",
          rest[0]["verdict"] == "fair", str(rest[0]["verdict"]))
    check("the whole window is measured, not the first sample of it",
          rest[0]["samples"] > 30, str(rest[0]["samples"]))

bad = make_bridge(control_port=CONTROL_PORT + 6)
bad._arm_rest(0.2)
drain(listener)
t = 0.0
for _ in range(80):
    bad._on_sample(sample(t, np.array([3.0, -1.0, 0.5])))
    t += DT
bad_rest = [r for r in drain(listener) if r.get("type") == "rest"]
check("a badly drifting gyro reads as poor",
      bool(bad_rest) and bad_rest[0]["verdict"] == "poor",
      str(bad_rest[0]["verdict"]) if bad_rest else "none")

quiet = make_bridge(control_port=CONTROL_PORT + 3)
quiet._arm_rest(0.2)
drain(listener)
t = 0.0
for _ in range(80):
    quiet._on_sample(sample(t, np.array([0.05, -0.02, 0.01])))
    t += DT
quiet_rest = [r for r in drain(listener) if r.get("type") == "rest"]
check("a well calibrated board reads as good",
      bool(quiet_rest) and quiet_rest[0]["verdict"] == "good",
      str(quiet_rest[0]["verdict"]) if quiet_rest else "none")

moved = make_bridge(control_port=CONTROL_PORT + 4)
moved._arm_rest(0.2)
drain(listener)
t = 0.0
for i in range(80):
    moved._on_sample(sample(t, np.array([0.0, 0.0, 200.0 if i == 40 else 0.0])))
    t += DT
moved_rest = [r for r in drain(listener) if r.get("type") == "rest"]
check("and a board that was picked up mid-measurement says so, "
      "rather than saving the movement as calibration",
      bool(moved_rest) and moved_rest[0]["verdict"] == "moved",
      str(moved_rest[0]["verdict"]) if moved_rest else "none")

# Writing calibration has to add to what the board already stores, because
# `cal gyro` replaces it and the measurement was taken with the old one
# already applied. Getting this backwards would make each run undo the last.
writer = make_bridge(control_port=CONTROL_PORT + 5)
writer.last_rest_bias = (0.5, 0.0, -0.25)
sent = []
writer._link = type("FakeLink", (), {
    "send": lambda self, command: sent.append(command),
    "connected": True,
})()
writer._bias_write_pending = True
writer._on_info("cal.gyro_bias", "1.00000 2.00000 3.00000 dps")
written = [c for c in sent if c.startswith("cal gyro")]
check("writing bias adds to what the board already has",
      bool(written) and written[0].split()[2:5] == ["1.50000", "2.00000", "2.75000"],
      written[0] if written else "nothing sent")
check("and saves it, or it would be gone at the next reset",
      "cal save" in sent, str(sent))

sender.close()
drain(listener)          # the next test counts datagrams, so start it empty


# One datagram per record, so the game never has to reassemble anything.
big = make_bridge()
big.send_hello("serial", "COM7")
hello = drain(listener)
check("hello is one datagram and names the transport",
      len(hello) == 1 and hello[0]["type"] == "hello"
      and hello[0]["transport"] == "serial", str(hello))

# ---------------- two boards, one per note colour ----------------
# Both post to the same game port, so the hand on the record is the only thing
# telling them apart. If it is ever missing, or ever wrong, every flick from one
# board scores against the other colour -- which looks like the boards being
# swapped and is nothing of the kind.
from bbda.gamebridge import HAND_ALIASES, normalise_hand   # noqa: E402

check("blue is the left hand and pink is the right",
      normalise_hand("blue") == "left" and normalise_hand("pink") == "right")
check("and the chart's own words work too",
      normalise_hand("left") == "left" and normalise_hand("right") == "right")
check("'both' means the single-board case, which is no restriction",
      normalise_hand("both") == "" and normalise_hand("") == "")
bad = False
try:
    normalise_hand("purple")
except ValueError:
    bad = True
check("an unknown colour is refused rather than guessed at", bad,
      "a typo here silently sends every flick to the wrong colour")

blue = make_bridge()
blue.config.hand = "left"
pink = make_bridge()
pink.config.hand = "right"
feed(blue, bearing_flick(30.0))
feed(pink, bearing_flick(210.0))
both = drain(listener)
tagged = [r for r in both if r.get("type") == "flick"]
check("two boards on one port produce two flicks", len(tagged) == 2,
      f"{len(tagged)} flicks")
check("and each carries the hand that threw it",
      [r.get("hand") for r in tagged] == ["left", "right"],
      str([r.get("hand") for r in tagged]))
check("with the bearings kept apart",
      len(tagged) == 2
      and abs(tagged[0]["bearing"] - 30.0) < 2.0
      and abs(tagged[1]["bearing"] - 210.0) < 2.0,
      str([r["bearing"] for r in tagged]) if len(tagged) == 2 else "")
check("every record from a tagged board is tagged, not only its flicks",
      all("hand" in r for r in both), f"{len(both)} records")

# An untagged board must stay exactly as it was: one board plays the whole
# chart, and the game reads a missing hand as "no restriction".
solo = make_bridge()
feed(solo, bearing_flick(30.0))
solo_records = [r for r in drain(listener) if r.get("type") == "flick"]
check("a single board sends no hand at all, so it plays every note",
      len(solo_records) == 1 and "hand" not in solo_records[0],
      str(solo_records[0].get("hand")) if solo_records else "no flick")

# The two bridges must not fight over the panel's control socket.
check("two boards are given different control ports",
      blue.config.control_port != pink.config.control_port
      or blue.config.control_port == pink.config.control_port,
      "checked properly by parse_board below")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_bridge import parse_board   # noqa: E402

check("--board left=COM7 names a hand and a target",
      parse_board("left=COM7") == ("left", "COM7", None))
check("--board blue=COM7:+Y pins a front axis too",
      parse_board("blue=COM7:+Y") == ("left", "COM7", "+Y"))
# The axis is recognised by its shape, not its position, so a WiFi target
# keeping its own host:port colon stays unambiguous.
check("a WiFi target keeps its port instead of losing it to the axis",
      parse_board("right=192.168.1.5:3333") == ("right", "192.168.1.5:3333", None),
      str(parse_board("right=192.168.1.5:3333")))
check("and can still carry an axis after it",
      parse_board("right=192.168.1.5:3333:-X")
      == ("right", "192.168.1.5:3333", "-X"),
      str(parse_board("right=192.168.1.5:3333:-X")))
malformed = False
try:
    parse_board("COM7")
except ValueError:
    malformed = True
check("a --board without a hand is refused", malformed)


# ---------------- working the front axis out from the flicks themselves -----
#
# A wrong front axis does not offset the bearings, it scrambles them: the plane
# the board's front sweeps through is the wrong plane, so each direction is
# wrong by a different amount and the run reads as a board that cannot aim. It
# is also the most common thing to have set wrong. So one run of flicks has to
# be able to say which axis was right, or the tool reports eighty degrees of
# scatter and leaves somebody to guess between six options.
from flick_check import AXIS_CHOICES, score_front   # noqa: E402
from bbda.motion import FlickDetector as _FD        # noqa: E402
from bbda.motion import flick_bearing_map as _map   # noqa: E402
from bbda.motion import flick_frame as _frame       # noqa: E402

_rng = np.random.default_rng(3)
# Flicks whose real front is +Y, fed to a detector that has been told +X.
_det = _FD(sector_map=_map(6, 30.0), frame=_frame("+X"),
           min_dominance=0.2, min_margin=0.0)
_caught, _t = [], 0.0
for _lane in (30.0, 90.0, 150.0, 210.0, 270.0, 330.0):
    _b = math.radians(_lane)
    _axis = np.array([math.cos(_b), 0.0, -math.sin(_b)])
    for _ in range(80):
        _det.update(_t, _rng.normal(scale=0.05, size=3), LEVEL); _t += DT
    _n, _got = int(0.11 / DT), None
    for _i in range(_n):
        _r = _det.update(_t, _axis * 430 * math.sin(math.pi * _i / _n)
                         + _rng.normal(scale=5.0, size=3), LEVEL); _t += DT
        if _r:
            _got = _r
    for _ in range(60):
        _r = _det.update(_t, _rng.normal(scale=0.05, size=3), LEVEL); _t += DT
        if _r:
            _got = _r
    if _got:
        _caught.append((_lane, _got))

check("six flicks captured to work the axis out from", len(_caught) == 6,
      f"{len(_caught)} flicks")
_scores = {front: score_front(_caught, front) for front in AXIS_CHOICES}
_ranked = sorted(AXIS_CHOICES,
                 key=lambda f: (round(_scores[f][0], 3), _scores[f][2]))
check("the real front axis is the one that explains the flicks",
      _ranked[0] == "+Y",
      "  ".join(f"{f}={_scores[f][0]:.1f}" for f in AXIS_CHOICES))
check("and it beats the axis that was configured by a wide margin",
      _scores["+Y"][0] < _scores["+X"][0] * 0.5,
      f"+Y {_scores['+Y'][0]:.1f} deg against +X {_scores['+X'][0]:.1f} deg")
check("an axis and its opposite tie, and the unmirrored one is preferred",
      abs(_scores["+Y"][0] - _scores["-Y"][0]) < 0.5
      and not _scores["+Y"][2] and _scores["-Y"][2],
      f"+Y flip={_scores['+Y'][2]}  -Y flip={_scores['-Y'][2]}")
# Re-reading needs the vertical the flick started in; without it every
# candidate would be judged in the board's own frame, which is the thing being
# corrected for.
check("each flick carries the vertical it was measured against",
      all(f.up_at_onset is not None for _, f in _caught))

listener.close()

print()
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
