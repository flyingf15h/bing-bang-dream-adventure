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

#: node_2d.gd's default layout: lane -> angle, measured counter-clockwise from
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
    """_nearest_sector() from node_2d.gd, restated."""
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

# Half the gesture, because the peak of a symmetric flick is its middle.
# Checked as a relationship rather than a constant precisely because it is not
# one: that it varies is the whole reason it has to travel with the event.
ok = bool(measured)
for duration_ms, lag_ms in measured:
    if abs(lag_ms - duration_ms / 2.0) > 12.0:
        ok = False
        check(f"lag for a {duration_ms:.0f} ms flick", False,
              f"got {lag_ms:.0f} ms, expected about {duration_ms / 2:.0f}")
check("the lag is about half the flick's duration", ok,
      "  ".join(f"{d:.0f}ms->{l:.0f}ms" for d, l in measured))
check("and it genuinely varies, so it cannot be a fixed offset",
      len({round(lag, 0) for _, lag in measured}) > 1,
      str([lag for _, lag in measured]))

# peak_t must precede t, or the game would reach forward in time.
lagged = make_bridge()
feed(lagged, bearing_flick(30.0, duration_s=0.12))
records_lag = [r for r in drain(listener) if r.get("type") == "flick"]
check("the peak is timestamped before the end of the event",
      bool(records_lag) and records_lag[0]["peak_t"] < records_lag[0]["t"])
check("the lag is never negative",
      bool(records_lag) and records_lag[0]["lag_ms"] >= 0.0)

# The other side of that: a flick straight up falls between two lanes, and the
# margin floor is what refuses it instead of picking one by rounding error.
edge = make_bridge()
feed(edge, bearing_flick(0.0))
check("a flick onto a lane boundary is refused rather than guessed",
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

# One datagram per record, so the game never has to reassemble anything.
big = make_bridge()
big.send_hello("serial", "COM7")
hello = drain(listener)
check("hello is one datagram and names the transport",
      len(hello) == 1 and hello[0]["type"] == "hello"
      and hello[0]["transport"] == "serial", str(hello))

listener.close()

print()
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
