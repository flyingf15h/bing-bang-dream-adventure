"""Self-checks for dead reckoning, flick detection and flip detection.

Runs without a display or hardware:  python tests/test_motion.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bbda.calibration import (
    Calibration,
    alignment_name,
    right_handed_alignments,
    snap_to_axis_alignment,
    solve_frame_from_gravity_and_moves,
    solve_frame_from_moves,
)
from bbda.motion import (
    G_MS2,
    DeadReckoning,
    FlickDetector,
    FlipDetector,
    KalmanDeadReckoning,
    QuickMoveDetector,
    SectorMap,
    StationaryDetector,
    flick_bearing_map,
    flick_frame,
    frame_sector_map,
    planarity,
)

fail = 0


def check(name, cond, extra=""):
    global fail
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fail += 1


DT = 0.005
LEVEL = np.array([0.0, 0.0, 1.0])
STILL = np.zeros(3)

# ---------------- Stationary detector ----------------
d = StationaryDetector()
for _ in range(40):
    d.update(LEVEL + np.random.normal(scale=0.002, size=3), np.random.normal(scale=0.3, size=3))
check("stationary when still", d.stationary)
for _ in range(40):
    d.update(LEVEL + np.array([0.5, 0, 0]), np.array([90.0, 0, 0]))
check("not stationary when moving", not d.stationary)

# An uncalibrated board: 6 dps of zero-rate offset and 4% of accelerometer
# gain error, both entirely ordinary for the part, and both sitting perfectly
# still. The absolute test cannot call this still -- correctly, on its own
# terms -- which is exactly why calibration cannot use the absolute test: it
# is fed raw readings because it is there to measure those offsets, so
# requiring them to be small already is requiring the answer as input.
RAW_LEVEL = LEVEL * 1.04
RAW_BIAS = np.array([3.5, -4.0, 2.0])          # 5.8 dps, well over the 2.5 default


def feed_still(detector, n=40, seed=1):
    rng = np.random.default_rng(seed)
    for _ in range(n):
        detector.update(RAW_LEVEL + rng.normal(scale=0.002, size=3),
                        RAW_BIAS + rng.normal(scale=0.05, size=3))
    return detector.stationary


check("absolute test cannot see an uncalibrated board as still",
      not feed_still(StationaryDetector()))
check("bias-blind test sees an uncalibrated still board as still",
      feed_still(StationaryDetector(bias_blind=True, accel_tolerance_g=0.02,
                                    gyro_tolerance_dps=1.5)))

# Bias-blindness must not become blindness. Movement still has to register.
d = StationaryDetector(bias_blind=True, accel_tolerance_g=0.02, gyro_tolerance_dps=1.5)
feed_still(d)
for i in range(40):
    d.update(RAW_LEVEL + np.array([0.3 * math.sin(i / 3), 0, 0]),
             RAW_BIAS + np.array([0, 0, 40.0 * math.sin(i / 4)]))
check("bias-blind test still sees movement", not d.stationary)

# The sanity bands are what catch the case a spread test cannot: readings that
# are steady but plainly not those of a resting board.
d = StationaryDetector(bias_blind=True, accel_tolerance_g=0.02, gyro_tolerance_dps=1.5)
for _ in range(40):
    d.update(np.array([0.0, 0.0, 1.8]), RAW_BIAS)     # steady 1.8 g
check("bias-blind test rejects a steady but impossible acceleration",
      not d.stationary)
d = StationaryDetector(bias_blind=True, accel_tolerance_g=0.02, gyro_tolerance_dps=1.5)
for _ in range(40):
    d.update(RAW_LEVEL, np.array([0.0, 0.0, 120.0]))  # steady spin
check("bias-blind test rejects a steady spin", not d.stationary)

# Offsets that cancel in the magnitude must not cancel in the verdict, which
# is why the gyro spread is taken per axis.
d = StationaryDetector(bias_blind=True, accel_tolerance_g=0.02, gyro_tolerance_dps=1.5)
feed_still(d)
for i in range(40):
    swing = 8.0 * math.sin(i / 3)
    d.update(RAW_LEVEL, RAW_BIAS + np.array([swing, -swing, 0.0]))
check("bias-blind test watches gyro axes, not just the magnitude",
      not d.stationary)

# ---------------- Dead reckoning ----------------
dr = DeadReckoning()
for _ in range(400):
    dr.update(np.eye(3), LEVEL, STILL, DT)
check("still board does not wander", float(np.linalg.norm(dr.state.position)) < 1e-3,
      f"{dr.state.position}")
check("still board reports stationary", dr.state.stationary)
check("drift estimate zero at rest", dr.state.drift_estimate_m == 0.0)

# A clean 1 m/s^2 push for 1 s then symmetric brake: net displacement ~1 m.
dr = DeadReckoning(velocity_damping=0.0)
dr.detector = StationaryDetector(accel_tolerance_g=1e-9, gyro_tolerance_dps=1e-9)
# Let the stationary detector fill its window first, exactly as a real board
# sitting on the desk would before you pick it up. Without this the first
# 0.1 s of the push is swallowed by the warm-up.
for _ in range(30):
    dr.update(np.eye(3), LEVEL, STILL, DT)
push = LEVEL + np.array([1.0 / 9.80665, 0, 0])
brake = LEVEL - np.array([1.0 / 9.80665, 0, 0])
for _ in range(200):
    dr.update(np.eye(3), push, STILL, DT)
for _ in range(200):
    dr.update(np.eye(3), brake, STILL, DT)
check("integrates a known push", abs(dr.state.position[0] - 1.0) < 0.02,
      f"x={dr.state.position[0]:.4f} m (expected ~1.0)")
check("velocity returns to ~0", abs(dr.state.speed) < 0.02, f"{dr.state.speed:.4f}")
check("path recorded", len(dr.path) > 10)

# ZUPT must clamp a velocity built up by a bias.
dr = DeadReckoning()
biased = LEVEL + np.array([0.02, 0, 0])
for _ in range(600):
    dr.update(np.eye(3), biased, STILL, DT)
check("ZUPT clamps bias-driven velocity", dr.state.speed < 1e-6, f"{dr.state.speed:.5f}")
check("ZUPT bounds bias-driven drift", float(np.linalg.norm(dr.state.position)) < 0.05,
      f"{np.linalg.norm(dr.state.position):.4f} m")

dr.reset_origin()
check("reset_origin zeroes position", float(np.linalg.norm(dr.state.position)) == 0.0)

# ---------------- Flick detection ----------------
def flick_profile(axis_index, peak_dps, duration_s=0.12, sign=1):
    """Half-sine angular-rate pulse on one axis."""
    n = int(duration_s / DT)
    out = []
    for i in range(n):
        v = np.zeros(3)
        v[axis_index] = sign * peak_dps * math.sin(math.pi * i / n)
        out.append(v)
    return out


for axis_index, axis_name in enumerate("xyz"):
    for sign in (+1, -1):
        det = FlickDetector()
        t = 0.0
        got = None
        for _ in range(20):
            det.update(t, STILL, LEVEL); t += DT
        for g in flick_profile(axis_index, 400, sign=sign):
            r = det.update(t, g, LEVEL); t += DT
            if r:
                got = r
        for _ in range(30):
            r = det.update(t, STILL, LEVEL); t += DT
            if r:
                got = r
        ok = got is not None and got.axis == axis_name and got.direction == sign
        check(f"flick {'+' if sign > 0 else '-'}{axis_name.upper()} detected", ok,
              f"got={got.label if got else None} peak={got.peak_dps:.0f}" if got else "none")

# Slow rotation must NOT be a flick.
det = FlickDetector()
t = 0.0
got = None
for _ in range(600):
    r = det.update(t, np.array([30.0, 0, 0]), LEVEL); t += DT
    if r:
        got = r
check("slow rotation is not a flick", got is None)

# Diagonal rotation must be rejected: no single axis dominates.
det = FlickDetector()
t = 0.0
got = None
for _ in range(20):
    det.update(t, STILL, LEVEL); t += DT
n = int(0.12 / DT)
for i in range(n):
    v = np.array([1.0, 1.0, 0.0]) / math.sqrt(2) * 400 * math.sin(math.pi * i / n)
    r = det.update(t, v, LEVEL); t += DT
    if r:
        got = r
for _ in range(30):
    r = det.update(t, STILL, LEVEL); t += DT
    if r:
        got = r
check("diagonal flick rejected by dominance", got is None,
      f"got {got.label} dominance={got.dominance:.2f}" if got else "")

# Noise below the trigger must not fire.
det = FlickDetector()
t = 0.0
got = None
for _ in range(2000):
    r = det.update(t, np.random.normal(scale=1.0, size=3), LEVEL); t += DT
    if r:
        got = r
check("gyro noise does not trigger", got is None)

# ---------------- Flip detection ----------------
det = FlipDetector()
t = 0.0
for _ in range(60):
    det.update(t, LEVEL, STILL); t += DT
check("initial face is +Z", det.current_face == "+Z", str(det.current_face))

flip = None
for _ in range(20):
    det.update(t, np.array([0.3, 0.3, 0.3]), np.array([200.0, 0, 0])); t += DT
for _ in range(60):
    r = det.update(t, np.array([0.0, 0.0, -1.0]), STILL); t += DT
    if r:
        flip = r
check("flip to -Z detected", flip is not None and flip.to_face == "-Z"
      and flip.from_face == "+Z",
      f"{flip.from_face}->{flip.to_face}" if flip else "none")

det2 = FlipDetector()
t = 0.0
for _ in range(60):
    det2.update(t, LEVEL, STILL); t += DT
extra = None
for _ in range(60):
    r = det2.update(t, LEVEL, STILL); t += DT
    if r:
        extra = r
check("no spurious flip when still", extra is None)

# ---------------- Axis alignment ----------------
options = right_handed_alignments()
check("24 right-handed alignments", len(options) == 24, str(len(options)))
check("all are rotations", all(abs(np.linalg.det(m) - 1) < 1e-9 for _n, m in options))
check("identity is first", np.allclose(options[0][1], np.eye(3)))
check("alignment_name round-trips",
      all(alignment_name(m) == n for n, m in options))

cal = Calibration()
cal.mount = np.array([[0, -1.0, 0], [1.0, 0, 0], [0, 0, 1.0]])  # +90 deg about Z
out = cal.apply_accel(np.array([1.0, 0.0, 0.0]))
check("mount rotates accel", np.allclose(out, [0, 1, 0]), str(out))
cal.gyro_bias = np.array([0.5, 0, 0])
out = cal.apply_gyro(np.array([1.5, 0.0, 0.0]))
check("mount applied after bias", np.allclose(out, [0, 1, 0]), str(out))
check("mount survives json", np.allclose(
    Calibration.from_dict(cal.to_dict()).mount, cal.mount))
check("mount not pushed to device",
      not any("mount" in c for c in cal.to_device_commands()))

# Round-trip through an actual file, not just a dict: easy mode's save and
# load buttons go through these two, and every field has to survive, mounting
# included -- the file is the only copy that carries it.
cal.accel_scale = np.array([1.01, 0.99, 1.02])
cal.mag_bias = np.array([-12.5, 3.25, 7.0])
cal.mag_soft = np.array([[1.02, 0.01, 0.0], [0.01, 0.98, 0.0], [0.0, 0.0, 1.0]])
with tempfile.TemporaryDirectory() as folder:
    saved = Path(folder) / "cal.json"
    cal.save(saved)
    back = Calibration.load(saved)
    check("calibration survives a save/load round trip", all(
        np.allclose(getattr(back, field), getattr(cal, field))
        for field in ("gyro_bias", "accel_bias", "accel_scale",
                      "mag_bias", "mag_soft", "mount")
    ))
    check("saved calibration is readable json",
          set(json.loads(saved.read_text()) ) == {
              "gyro_bias", "accel_bias", "accel_scale", "mag_bias",
              "mag_soft", "mount"})


# ---------------- Kalman dead reckoning ----------------
def settled_kalman():
    """A filter with a detector tight enough to call the synthetic data still."""
    kf = KalmanDeadReckoning()
    kf.detector = StationaryDetector(accel_tolerance_g=1e-9, gyro_tolerance_dps=1e-9)
    for _ in range(30):
        kf.update(np.eye(3), LEVEL, STILL, DT)
    return kf


kf = KalmanDeadReckoning()
for _ in range(400):
    kf.update(np.eye(3), LEVEL, STILL, DT)
check("kalman: still board does not wander",
      float(np.linalg.norm(kf.state.position)) < 1e-6, f"{kf.state.position}")
check("kalman: uncertainty stays small while still", kf.state.position_sigma < 0.02,
      f"{kf.state.position_sigma:.4f} m")

# The same 1 m push the simple estimator is checked against, so the two are
# directly comparable.
kf = settled_kalman()
push = LEVEL + np.array([1.0 / G_MS2, 0, 0])
brake = LEVEL - np.array([1.0 / G_MS2, 0, 0])
for _ in range(200):
    kf.update(np.eye(3), push, STILL, DT)
moving_sigma = kf.state.position_sigma
for _ in range(200):
    kf.update(np.eye(3), brake, STILL, DT)
check("kalman: integrates a known push", abs(kf.state.position[0] - 1.0) < 0.02,
      f"x={kf.state.position[0]:.4f} m (expected ~1.0)")
check("kalman: uncertainty grows while moving", moving_sigma > kf.state.position_sigma
      or moving_sigma > 0.02, f"{moving_sigma:.4f} m")

# Trapezoidal integration is exact when acceleration varies linearly over the
# step; rectangular integration leaves ~a*dt/2 behind on every one of them.
# A constant-jerk ramp is where that shows, because nothing cancels it.
simple = DeadReckoning(velocity_damping=0.0)
simple.detector = StationaryDetector(accel_tolerance_g=1e-9, gyro_tolerance_dps=1e-9)
kf2 = settled_kalman()
for _ in range(30):
    simple.update(np.eye(3), LEVEL, STILL, DT)
JERK, RAMP_STEPS = 2.0, 200
for i in range(RAMP_STEPS):
    a = LEVEL + np.array([JERK * (i + 1) * DT / G_MS2, 0, 0])
    kf2.update(np.eye(3), a, STILL, DT)
    simple.update(np.eye(3), a, STILL, DT)
span = RAMP_STEPS * DT
exact_v, exact_p = JERK * span ** 2 / 2, JERK * span ** 3 / 6
check("kalman: velocity is exact under constant jerk",
      abs(kf2.state.velocity[0] - exact_v) < 1e-9,
      f"{kf2.state.velocity[0]:.9f} vs {exact_v:.9f}")
check("kalman: constant-jerk position beats the rectangular form",
      abs(kf2.state.position[0] - exact_p) < 0.01 * abs(simple.state.position[0] - exact_p),
      f"kalman {abs(kf2.state.position[0] - exact_p):.2e} m, "
      f"simple {abs(simple.state.position[0] - exact_p):.2e} m")

# ...and equally worth recording that on a symmetric move the two are level,
# so nobody reads more into the integration scheme than it deserves.
simple = DeadReckoning(velocity_damping=0.0)
simple.detector = StationaryDetector(accel_tolerance_g=1e-9, gyro_tolerance_dps=1e-9)
kf3 = settled_kalman()
for _ in range(30):
    simple.update(np.eye(3), LEVEL, STILL, DT)
for _ in range(200):
    kf3.update(np.eye(3), push, STILL, DT)
    simple.update(np.eye(3), push, STILL, DT)
for _ in range(200):
    kf3.update(np.eye(3), brake, STILL, DT)
    simple.update(np.eye(3), brake, STILL, DT)
check("kalman: on a symmetric move the schemes agree closely",
      abs(kf3.state.position[0] - simple.state.position[0]) < 1e-3,
      f"kalman {kf3.state.position[0]:.6f} vs simple {simple.state.position[0]:.6f}")

# A steady accelerometer bias must be learnt, not just clamped away.
kf = KalmanDeadReckoning()
biased = LEVEL + np.array([0.02, 0, 0])
for _ in range(2000):
    kf.update(np.eye(3), biased, STILL, DT)
check("kalman: learns a standing accel bias",
      abs(kf.state.accel_bias[0] - 0.02 * G_MS2) < 0.02 * G_MS2 * 0.25,
      f"{kf.state.accel_bias[0]:.4f} of {0.02 * G_MS2:.4f} m/s^2")
check("kalman: ZUPT bounds bias-driven drift",
      float(np.linalg.norm(kf.state.position)) < 0.01,
      f"{np.linalg.norm(kf.state.position):.5f} m")

# The stop must pull position back, not merely stop it moving. A bias applied
# only while "moving" builds a position error the ZUPT then has to remove.
kf = KalmanDeadReckoning()
kf.detector = StationaryDetector(accel_tolerance_g=0.001, gyro_tolerance_dps=1e-9)
for _ in range(40):
    kf.update(np.eye(3), LEVEL, STILL, DT)
for _ in range(300):
    kf.update(np.eye(3), LEVEL + np.array([0.05, 0, 0]), STILL, DT)
before = abs(kf.state.position[0])
for _ in range(200):
    kf.update(np.eye(3), LEVEL, STILL, DT)
check("kalman: a stop pulls the position back", abs(kf.state.position[0]) < before,
      f"{before:.4f} m -> {abs(kf.state.position[0]):.4f} m")
check("kalman: reset_origin zeroes position",
      (kf.reset_origin(), float(np.linalg.norm(kf.state.position)))[1] == 0.0)

kf = KalmanDeadReckoning(zupt_enabled=False)
for _ in range(600):
    kf.update(np.eye(3), LEVEL + np.array([0.02, 0, 0]), STILL, DT)
check("kalman: without ZUPT it drifts, as it must",
      float(np.linalg.norm(kf.state.position)) > 0.1,
      f"{np.linalg.norm(kf.state.position):.3f} m")

# ---------------- Sector maps ----------------
sectors = SectorMap(6)
check("6 sectors are 60 degrees wide", abs(sectors.width_deg - 60.0) < 1e-9)
check("sector labels name their centre",
      sectors.labels == ["0°", "60°", "120°", "180°", "240°", "300°"],
      str(sectors.labels))
check("sector 0 is centred on the offset, not started at it",
      sectors.sector_of(0.0).index == 0 and sectors.sector_of(0.0).margin == 1.0)
check("a direction just inside a sector lands in it",
      sectors.sector_of(29.0).index == 0 and sectors.sector_of(31.0).index == 1)
check("a boundary direction has no margin", sectors.sector_of(30.0).margin < 1e-9)
check("sectors wrap", sectors.sector_of(359.0).index == 0
      and sectors.sector_of(-1.0).index == 0)
check("4 sectors put the axes at the centres",
      [SectorMap(4).sector_of(a).index for a in (0, 90, 180, 270)] == [0, 1, 2, 3])
check("frame sectors are named in words",
      frame_sector_map(4).labels == ["forward", "left", "back", "right"])
check("frame sectors work for 6 too",
      frame_sector_map(6).sector_of(60.0).label == "forward-left")
try:
    SectorMap(1)
    check("a one-sector map is refused", False)
except ValueError:
    check("a one-sector map is refused", True)
try:
    SectorMap(4, labels=["a", "b"])
    check("wrong label count is refused", False)
except ValueError:
    check("wrong label count is refused", True)

check("planarity of an in-plane vector is 1",
      abs(planarity(np.array([3.0, 4.0, 0.0]), (0, 1)) - 1.0) < 1e-9)
check("planarity of a normal vector is 0",
      planarity(np.array([0.0, 0.0, 5.0]), (0, 1)) < 1e-9)

check("sector_of_vector reads the plane's angle",
      SectorMap(4).sector_of_vector(np.array([0.0, 2.0, 9.0]), (0, 1)).index == 1)

# ---------------- Flick detection, sector mode ----------------
def planar_flick(angle_deg, peak_dps=400, duration_s=0.12):
    """Half-sine rotation pulse in the X-Y plane at a given angle."""
    n = int(duration_s / DT)
    axis = np.array([math.cos(math.radians(angle_deg)),
                     math.sin(math.radians(angle_deg)), 0.0])
    return [axis * peak_dps * math.sin(math.pi * i / n) for i in range(n)]


def run_flick(detector, pulses):
    t = 0.0
    got = None
    for _ in range(20):
        detector.update(t, STILL, LEVEL); t += DT
    for g in pulses:
        r = detector.update(t, g, LEVEL); t += DT
        if r:
            got = r
    for _ in range(40):
        r = detector.update(t, STILL, LEVEL); t += DT
        if r:
            got = r
    return got


for count in (4, 6, 8):
    ok = True
    for index in range(count):
        centre = index * 360.0 / count
        got = run_flick(FlickDetector(sector_map=SectorMap(count)),
                        planar_flick(centre))
        if got is None or got.sector is None or got.sector.index != index:
            ok = False
    check(f"flick resolves all {count} sectors", ok)

got = run_flick(FlickDetector(sector_map=SectorMap(6)), planar_flick(30.0))
check("flick on a sector boundary is rejected", got is None,
      f"got {got.label}" if got else "")

got = run_flick(FlickDetector(sector_map=SectorMap(6)),
                [np.array([0.0, 0.0, 1.0]) * 400 * math.sin(math.pi * i / 24)
                 for i in range(24)])
check("flick out of the sector plane is rejected", got is None,
      f"got {got.label}" if got else "")

# Same rotation, but now the plane being divided contains it: about +Z reads
# as 90 degrees in the Y-Z plane, which is the centre of sector 1 of four.
got = run_flick(FlickDetector(sector_map=SectorMap(4), plane=(1, 2)),
                [np.array([0.0, 0.0, 1.0]) * 400 * math.sin(math.pi * i / 24)
                 for i in range(24)])
check("the same flick is accepted once the plane contains it",
      got is not None and got.sector is not None and got.sector.index == 1,
      f"got {got.label}" if got else "none")

got = run_flick(FlickDetector(), planar_flick(0.0))
check("axis mode still reports an axis and no sector",
      got is not None and got.sector is None and got.axis == "x" and got.direction == 1,
      f"got {got.label}" if got else "none")

# ---------------- Flick detection, degrees from the front ----------------
# The frame the mounting step leaves behind: X forward, Y left, Z up. "Up" and
# "right" below are the board's own, which is what a person flicking it means.
UP = np.array([0.0, 0.0, 1.0])
RIGHT = np.array([0.0, -1.0, 0.0])
FRONT = np.array([1.0, 0.0, 0.0])

# Pitch up, pitch down, yaw right, yaw left, roll either way. What a player
# calls a flick up is the first of these, and the rotation is measured about
# the sideways axis -- naming a flick after that axis is the mistake the whole
# mode exists to avoid.
PITCH_UP = np.array([0.0, -1.0, 0.0])
PITCH_DOWN = -PITCH_UP
YAW_RIGHT = np.array([0.0, 0.0, -1.0])
YAW_LEFT = -YAW_RIGHT
ROLL = FRONT.copy()

front_frame = flick_frame("+X")
check("the frame's left completes a right-handed set",
      np.allclose(front_frame.left, [0.0, 1.0, 0.0]), str(front_frame.left))
check("a pitch up sweeps the front up",
      np.allclose(front_frame.sweep(PITCH_UP), UP))
check("a yaw right sweeps the front right",
      np.allclose(front_frame.sweep(YAW_RIGHT), RIGHT))
check("a roll about the front sweeps nowhere",
      np.allclose(front_frame.sweep(ROLL * 250.0), np.zeros(3)))
check("a pitch is all swing", abs(front_frame.swing_fraction(PITCH_UP) - 1.0) < 1e-9)
check("a roll is no swing at all", front_frame.swing_fraction(ROLL * 250.0) < 1e-9)

check("a pitch up is 0 degrees",
      abs(front_frame.bearing_deg(PITCH_UP)) < 1e-9)
check("a pitch down is 180 degrees",
      abs(front_frame.bearing_deg(PITCH_DOWN) - 180.0) < 1e-9)
check("a yaw right is 90 degrees",
      abs(front_frame.bearing_deg(YAW_RIGHT) - 90.0) < 1e-9)
check("a yaw left is 270 degrees",
      abs(front_frame.bearing_deg(YAW_LEFT) - 270.0) < 1e-9)

# The same physical gesture on a board whose long axis is Y, which is what an
# uncalibrated mounting leaves you with. Naming +X the front there would call
# this pitch a roll, and the roll a flick upwards.
side_frame = flick_frame("+Y")
check("a board fronted on +Y reads its own pitch as up",
      abs(side_frame.bearing_deg(np.array([1.0, 0.0, 0.0]))) < 1e-9)
check("and reads a roll about that front as no direction",
      side_frame.swing_fraction(np.array([0.0, 250.0, 0.0])) < 1e-9)
check("the front axis choice is what separates the two",
      front_frame.swing_fraction(np.array([0.0, 250.0, 0.0])) > 0.99)

check("bearings are named by their degrees",
      flick_bearing_map(6).labels == ["0°", "60°", "120°", "180°", "240°", "300°"],
      str(flick_bearing_map(6).labels))


def bearing_flick(bearing_deg, peak_dps=400, duration_s=0.12):
    """A rotation pulse that sends the board's front to a given bearing.

    Inverts the sweep: to send the front towards ``up cos b + right sin b``
    the board must turn about ``-(y cos b + z sin b)``.
    """
    n = int(duration_s / DT)
    axis = -np.array([0.0,
                      math.cos(math.radians(bearing_deg)),
                      math.sin(math.radians(bearing_deg))])
    return [axis * peak_dps * math.sin(math.pi * i / n) for i in range(n)]


def bearing_detector(count=6, front="+X"):
    return FlickDetector(sector_map=flick_bearing_map(count),
                         frame=flick_frame(front))


ok = True
for index, bearing in enumerate(range(0, 360, 60)):
    got = run_flick(bearing_detector(), bearing_flick(bearing))
    if (got is None or got.sector is None or got.sector.index != index
            or got.sector.label != f"{bearing}°"):
        ok = False
        check(f"flick at {bearing}° from the front", False,
              f"got {got.label}" if got else "none")
check("six bearings resolve, 0 up through 300 up-and-left", ok)

# The gesture a player would call "up" has to come out as 0, not as the 90 the
# rotation axis alone would say -- that is the whole point of the mode.
got = run_flick(bearing_detector(), bearing_flick(0.0))
check("flicking the front upwards reads as 0 degrees",
      got is not None and got.sector is not None
      and abs(got.sector.angle_deg) < 1e-6, f"got {got.label}" if got else "none")
got = run_flick(bearing_detector(4), bearing_flick(90.0))   # 90 is a boundary of six
check("flicking the front to the right reads as 90 degrees",
      got is not None and got.sector is not None
      and abs(got.sector.angle_deg - 90.0) < 1e-6,
      f"got {got.label}" if got else "none")

# Up and down have to be the two ends of one gesture, not two unrelated ones.
up = run_flick(bearing_detector(), bearing_flick(0.0))
down = run_flick(bearing_detector(), bearing_flick(180.0))
check("up and down are opposite ends of the same pitch",
      up is not None and down is not None
      and up.axis == down.axis == "y" and up.direction == -down.direction,
      f"{up.label if up else 'none'} / {down.label if down else 'none'}")

got = run_flick(bearing_detector(), bearing_flick(30.0))
check("a flick between two bearings is refused", got is None,
      f"got {got.label}" if got else "")

# A roll sends the front nowhere, so there is no direction to report. Getting
# this wrong is what makes a roll one way read as "up" and the other as "down".
for sign in (+1, -1):
    got = run_flick(bearing_detector(),
                    [ROLL * sign * 400 * math.sin(math.pi * i / 24)
                     for i in range(24)])
    check(f"a roll {'one way' if sign > 0 else 'the other'} is not a direction",
          got is None, f"got {got.label}" if got else "")

# ---------------- Quick movement ----------------
def shove(direction, distance=0.30, duration=0.5):
    """Acceleration profile of a move that starts and ends at rest.

    a(t) = A sin(2 pi t / T) integrates to a velocity bump that returns to
    zero, and to a net displacement of A T^2 / (2 pi) -- so `distance` is what
    the board actually travels.
    """
    n = int(duration / DT)
    amplitude = distance * 2 * math.pi / duration ** 2
    unit = np.asarray(direction, dtype=float)
    unit = unit / np.linalg.norm(unit)
    return [unit * amplitude * math.sin(2 * math.pi * i / n) for i in range(n)]


def run_move(detector, profile, rest_before=60, rest_after=80):
    t = 0.0
    got = None
    for _ in range(rest_before):
        detector.update(t, np.zeros(3), DT); t += DT
    for a in profile:
        r = detector.update(t, a, DT); t += DT
        if r:
            got = r
    for _ in range(rest_after):
        r = detector.update(t, np.zeros(3), DT); t += DT
        if r:
            got = r
    return got


for name, direction in (("forward", [1, 0, 0]), ("left", [0, 1, 0]),
                        ("back", [-1, 0, 0]), ("right", [0, -1, 0])):
    got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), shove(direction))
    check(f"quick move {name} detected and named",
          got is not None and got.label == name, f"got {got.label if got else None}")

got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), shove([0, 0, 1]))
check("a lift is reported as up, not as a sector",
      got is not None and got.label == "up" and got.sector is None and got.vertical == 1,
      f"got {got.label if got else None}")
got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), shove([0, 0, -1]))
check("a drop is reported as down",
      got is not None and got.label == "down" and got.vertical == -1,
      f"got {got.label if got else None}")

got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), shove([1, 0, 0]))
check("quick move reports a plausible distance",
      got is not None and 0.15 < got.distance < 0.32, f"{got.distance:.3f} m" if got else "")
check("quick move reports a plausible peak speed",
      got is not None and 0.5 < got.peak_speed < 2.5,
      f"{got.peak_speed:.3f} m/s" if got else "")

got = run_move(QuickMoveDetector(sector_map=frame_sector_map(6)),
               shove([math.cos(math.radians(60)), math.sin(math.radians(60)), 0]))
check("6-direction move naming", got is not None and got.label == "forward-left",
      f"got {got.label if got else None}")

got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)),
               shove([math.cos(math.radians(45)), math.sin(math.radians(45)), 0]))
check("a move between two sectors is rejected", got is None,
      f"got {got.label}" if got else "")

# The direction comes from the peak of the integrated velocity precisely so a
# hard stop cannot make the move look like it went backwards.
asymmetric = ([np.array([6.0, 0.0, 0.0])] * 60      # gentle push
              + [np.array([-18.0, 0.0, 0.0])] * 20  # sharp brake
              + [np.zeros(3)] * 5)
got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), asymmetric)
check("a hard stop does not reverse the reported direction",
      got is not None and got.label == "forward", f"got {got.label if got else None}")

# Starting a move before the board has been still is the case that used to be
# read backwards, because the integration would start mid-flight.
got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)), shove([1, 0, 0]),
               rest_before=2)
check("a move that never started from rest is refused", got is None,
      f"got {got.label}" if got else "")

got = run_move(QuickMoveDetector(sector_map=frame_sector_map(4)),
               shove([1, 0, 0], distance=0.004, duration=0.2))
check("a nudge is not a movement", got is None, f"got {got.label}" if got else "")

got = run_move(QuickMoveDetector(sector_map=None), shove([0, 1, 0]))
check("axis mode names the nearest board axis",
      got is not None and got.label == "+Y" and got.sector is None,
      f"got {got.label if got else None}")

detector = QuickMoveDetector(sector_map=frame_sector_map(4))
t = 0.0
for _ in range(3000):
    r = detector.update(t, np.random.normal(scale=0.3, size=3), DT); t += DT
    if r:
        break
check("accelerometer noise is not a movement", r is None)

# The thresholds easy mode's orientation step runs the detector at live in the
# wizard, but what they have to accept is a physical question about how people
# push a circuit board across a desk, so it is pinned down here rather than
# left to be discovered by someone shoving one about and getting nothing.
try:
    from bbda.wizard import SLIDE_DETECTOR
except ImportError:              # PySide6 absent; nothing else here needs it
    SLIDE_DETECTOR = None

if SLIDE_DETECTOR is None:
    print("SKIP easy-mode slide thresholds (PySide6 not installed)")
else:
    for dist, dur, want in ((0.03, 0.40, True),    # a small flick of a shove
                            (0.05, 0.80, True),
                            (0.10, 1.20, True),    # slow and deliberate
                            (0.20, 1.80, True),    # slow and long
                            (0.002, 0.30, False)): # a 2 mm nudge is nothing
        got = run_move(QuickMoveDetector(**SLIDE_DETECTOR),
                       shove([1, 0, 0], distance=dist, duration=dur))
        check(f"easy-mode slide accepts {dist * 100:.0f} cm over {dur:.1f} s"
              if want else "easy-mode slide still refuses a 2 mm nudge",
              (got is not None) == want and (got is None or got.label == "+X"),
              f"got {got.label if got else None}")

    # Leniency must not become credulity: the step runs for as long as someone
    # takes to read the instructions, and must not invent a slide meanwhile.
    lenient = QuickMoveDetector(**SLIDE_DETECTOR)
    t = 0.0
    spurious = None
    noise = np.random.default_rng(5)
    for _ in range(12000):       # a minute at 200 Hz
        spurious = lenient.update(t, noise.normal(scale=0.05, size=3), DT) or spurious
        t += DT
    check("easy-mode slide thresholds do not fire on a minute of noise",
          spurious is None, f"got {spurious.label}" if spurious else "")

# ---------------- Orientation from movements ----------------
def measured_moves(mount, noise=0.0, seed=0):
    """What each movement looks like in board axes for a given mounting."""
    rng = np.random.default_rng(seed)
    out = {}
    for name, target in (("forward", [1, 0, 0]), ("left", [0, 1, 0]),
                         ("right", [0, -1, 0]), ("up", [0, 0, 1]),
                         ("down", [0, 0, -1])):
        v = mount.T @ np.array(target, dtype=float)
        if noise:
            v = v + rng.normal(scale=noise, size=3)
        out[name] = v / np.linalg.norm(v)
    return out


for name, truth in right_handed_alignments():
    result = solve_frame_from_moves(measured_moves(truth))
    if not (result.ok and np.allclose(result.snapped, truth)):
        check(f"frame solve recovers {name}", False)
        break
else:
    check("frame solve recovers all 24 mountings", True)

result = solve_frame_from_moves(measured_moves(np.eye(3), noise=0.06, seed=3))
check("frame solve survives sloppy movements",
      result.ok and np.allclose(result.snapped, np.eye(3)), result.message)
check("frame solve reports per-movement residuals",
      len(result.residuals_deg) == 5 and result.worst_deg < 15.0,
      f"worst {result.worst_deg:.1f} deg")
check("frame solve returns a rotation, never a reflection",
      abs(np.linalg.det(result.matrix) - 1.0) < 1e-9,
      f"det {np.linalg.det(result.matrix):.6f}")

check("frame solve refuses a single movement",
      not solve_frame_from_moves({"forward": [1, 0, 0]}).ok)
check("frame solve refuses parallel movements only",
      not solve_frame_from_moves({"forward": [1, 0, 0], "back": [-1, 0, 0]}).ok)
check("frame solve ignores unknown movement names",
      not solve_frame_from_moves({"sideways": [1, 0, 0], "up": [0, 0, 1]}).ok)

# A board mounted genuinely crooked must snap to the nearest square mounting
# and say how far off it was, rather than pretending it was square.
angle = math.radians(12.0)
tilted = np.array([[math.cos(angle), -math.sin(angle), 0.0],
                   [math.sin(angle), math.cos(angle), 0.0],
                   [0.0, 0.0, 1.0]])
result = solve_frame_from_moves(measured_moves(tilted))
check("a crooked mounting snaps to the nearest square one",
      np.allclose(result.snapped, np.eye(3)), result.snapped_name)
check("a crooked mounting reports how crooked it is",
      abs(result.snap_error_deg - 12.0) < 1.0, f"{result.snap_error_deg:.1f} deg")

name, matrix, error = snap_to_axis_alignment(np.eye(3))
check("snapping an exact alignment costs no angle", error < 1e-6, f"{error:.6f}")

# ---------------- Orientation from gravity plus slides ----------------
# What easy mode actually uses. Gravity fixes the vertical axis exactly and for
# free, so the slides only have to pick a horizontal one -- which is why every
# case below is allowed to be far sloppier than solve_frame_from_moves accepts.
def in_board(mount, display):
    """A display-frame direction, expressed in board axes."""
    v = mount.T @ np.array(display, dtype=float)
    return v / np.linalg.norm(v)


def gravity_and_slides(mount, names=("forward", "right")):
    targets = {"forward": [1, 0, 0], "back": [-1, 0, 0],
               "left": [0, 1, 0], "right": [0, -1, 0]}
    return (in_board(mount, [0, 0, 1]),
            {name: in_board(mount, targets[name]) for name in names})


for name, truth in right_handed_alignments():
    gravity, slides = gravity_and_slides(truth)
    result = solve_frame_from_gravity_and_moves(gravity, slides)
    if not (result.ok and np.allclose(result.snapped, truth)):
        check(f"gravity solve recovers {name}", False, result.message)
        break
else:
    check("gravity solve recovers all 24 mountings from two slides", True)

# One slide is a complete answer, because gravity supplied the other axis.
for name, truth in right_handed_alignments():
    gravity, slides = gravity_and_slides(truth, names=("forward",))
    result = solve_frame_from_gravity_and_moves(gravity, slides)
    if not (result.ok and np.allclose(result.snapped, truth)):
        check(f"gravity solve recovers {name} from one slide", False, result.message)
        break
else:
    check("gravity solve recovers all 24 mountings from one slide", True)


def rot_about(axis, degrees):
    u = np.asarray(axis, dtype=float)
    u = u / np.linalg.norm(u)
    a = math.radians(degrees)
    skew = np.array([[0, -u[2], u[1]], [u[2], 0, -u[0]], [-u[1], u[0], 0]])
    return np.eye(3) + math.sin(a) * skew + (1 - math.cos(a)) * (skew @ skew)


# A slide 35 degrees off course, and tilted 25 degrees out of the table, still
# lands on the right mapping. That leniency is the point of the method: the
# answer is one of 24, so anything inside 45 degrees is the same answer.
crooked = rot_about([0, 0, 1], 35.0) @ rot_about([0, 1, 0], 25.0)
gravity, slides = gravity_and_slides(np.eye(3), names=("forward",))
slides["forward"] = crooked @ slides["forward"]
result = solve_frame_from_gravity_and_moves(gravity, slides)
check("a badly aimed slide still names the right axes",
      result.ok and np.allclose(result.snapped, np.eye(3)),
      f"{result.snapped_name}, {result.snap_error_deg:.0f} deg off")

# Tilting the board itself must not move the answer either: gravity follows
# the board, and the slide is measured relative to gravity.
gravity, slides = gravity_and_slides(np.eye(3))
lean = rot_about([1, 0, 0], 8.0)
result = solve_frame_from_gravity_and_moves(
    lean @ gravity, {k: lean @ v for k, v in slides.items()})
check("a table that is not level does not change the answer",
      result.ok and np.allclose(result.snapped, np.eye(3)), result.snapped_name)

# A slide that was really a lift says nothing about forward and is dropped
# rather than folded in as if it did.
gravity, slides = gravity_and_slides(np.eye(3))
slides["right"] = np.array([0.0, 0.0, 1.0])
result = solve_frame_from_gravity_and_moves(gravity, slides)
check("a vertical slide is left out, not averaged in",
      result.ok and np.allclose(result.snapped, np.eye(3))
      and "right" not in result.residuals_deg, result.message)

gravity, _ = gravity_and_slides(np.eye(3))
check("no usable slide is refused, not guessed",
      not solve_frame_from_gravity_and_moves(
          gravity, {"forward": [0, 0, 1]}).ok)
check("no gravity reading is refused",
      not solve_frame_from_gravity_and_moves(np.zeros(3), slides).ok)
check("gravity solve ignores unknown slide names",
      not solve_frame_from_gravity_and_moves(gravity, {"sideways": [1, 0, 0]}).ok)

# Two slides that contradict each other are reported as such rather than
# quietly averaged into a direction neither of them supports.
result = solve_frame_from_gravity_and_moves(
    np.array([0.0, 0.0, 1.0]),
    {"forward": [1.0, 0.0, 0.0], "right": [-1.0, 0.0, 0.0]})
check("contradicting slides are reported, not averaged",
      not result.ok and result.worst_deg > 40.0, result.message)

result = solve_frame_from_gravity_and_moves(*gravity_and_slides(np.eye(3)))
check("gravity solve returns a rotation, never a reflection",
      abs(np.linalg.det(result.matrix) - 1.0) < 1e-9,
      f"det {np.linalg.det(result.matrix):.6f}")

print()
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
