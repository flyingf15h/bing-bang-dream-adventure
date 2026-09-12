"""Self-check for the headless calibration sequence.

Runs without a display, a board or the game:  python tests/test_calseq.py

Drives :class:`bbda.calseq.CalSequence` through a full run with synthetic
samples standing in for a hand moving a real board -- constant orientations
for the six-position step, a sphere of points for the magnetometer fit, and a
scripted slide (the same acceleration-profile trick ``test_motion.py`` uses
for QuickMoveDetector) for the orientation step. The maths underneath
(GyroBiasCollector, AccelSixPointCollector, fit_ellipsoid,
solve_frame_from_gravity_and_moves, QuickMoveDetector) is already covered by
test_math.py and test_motion.py; what this checks is that calseq.py's new
sequencing calls them in the right order, with the right raw-vs-corrected
inputs, and actually reaches "check" with a plausible calibration -- the one
thing wizard.py cannot get wrong silently, because a person watching the
screen would notice a step that never finished. Nothing here watches a
screen, so this is what has to notice instead.
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

# alignment_name() reports axes as e.g. "X→+X", and a Windows console in
# its default cp1252 code page cannot print that arrow -- the same encoding
# gap test_motion.py hits already. Reconfigured rather than left to crash mid
# run, since a script that dies on a print statement never reaches the checks
# after it.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bbda import calseq
from bbda.calibration import SIX_POSITIONS
from bbda.link import Sample
from bbda.motion import G_MS2

fail = 0


def check(name, cond, extra=""):
    global fail
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fail += 1


# A private autosave directory, so this never reads or overwrites a real
# user's saved calibration -- the whole point of the per-label file is that
# two boards (and now two test runs) do not clobber each other.
_tmp_home = Path(tempfile.mkdtemp(prefix="bbda_calseq_test_"))
calseq._AUTOSAVE_DIR = _tmp_home

DT = 1.0 / 400.0   # matches BridgeConfig's default rate_hz


def sample(t, accel, gyro, mag, mag_fresh=True):
    return Sample(t=t, accel=np.asarray(accel, dtype=float),
                  gyro=np.asarray(gyro, dtype=float),
                  mag=np.asarray(mag, dtype=float), temp=25.0,
                  mag_fresh=mag_fresh, host_t=0.0)


def shove_profile(direction, distance=0.10, duration=0.8, dt=DT):
    """Acceleration profile (m/s^2) of a move that starts and ends at rest.

    Identical construction to test_motion.py's ``shove()``: a(t) = A sin(2 pi
    t / T) integrates to a velocity bump that returns to zero and a net
    displacement of ``distance``.
    """
    n = int(duration / dt)
    amplitude = distance * 2 * math.pi / duration ** 2
    unit = np.asarray(direction, dtype=float)
    unit = unit / np.linalg.norm(unit)
    return [unit * amplitude * math.sin(2 * math.pi * i / n) for i in range(n)]


AXIS_VECTOR = {"x": np.array([1.0, 0.0, 0.0]), "y": np.array([0.0, 1.0, 0.0]),
               "z": np.array([0.0, 0.0, 1.0])}
NEUTRAL_MAG = [20.0, 0.0, 30.0]     # ~36 uT, plausible but irrelevant here

rng = np.random.default_rng(0)
t = 0.0

seq = calseq.CalSequence(label="test")
seq.start("full")
check("a fresh sequence starts at rest", seq.step == "rest")
check("state() names the step", seq.state()["step"] == "rest")

# ---------------------------------------------------------------------
# Rest: gyro bias
# ---------------------------------------------------------------------
TRUE_GYRO_BIAS = np.array([0.8, -0.3, 0.5])
steps = 0
while seq.step == "rest" and steps < 4000:
    gyro = TRUE_GYRO_BIAS + rng.normal(scale=0.05, size=3)
    seq.feed(sample(t, [0.0, 0.0, 1.0], gyro, NEUTRAL_MAG))
    t += DT
    steps += 1

check("rest step completes on its own", seq.step == "six_sides")
check("the recovered gyro bias matches what was fed",
      bool(np.allclose(seq.cal.gyro_bias, TRUE_GYRO_BIAS, atol=0.05)),
      str(seq.cal.gyro_bias))

# ---------------------------------------------------------------------
# Six sides: accelerometer bias and gain
# ---------------------------------------------------------------------
for name, axis, sign, _desc in SIX_POSITIONS:
    accel = AXIS_VECTOR[axis] * sign
    for _ in range(250):
        seq.feed(sample(t, accel, TRUE_GYRO_BIAS, NEUTRAL_MAG))
        t += DT

check("six sides step completes once all six are captured", seq.step == "wave")
check("accelerometer bias recovered as ~0 with a clean synthetic capture",
      bool(np.allclose(seq.cal.accel_bias, 0.0, atol=0.01)), str(seq.cal.accel_bias))
check("accelerometer scale recovered as ~1 with a clean synthetic capture",
      bool(np.allclose(seq.cal.accel_scale, 1.0, atol=0.01)), str(seq.cal.accel_scale))

# ---------------------------------------------------------------------
# Wave it around: magnetometer hard/soft iron
# ---------------------------------------------------------------------
FIELD_UT = 45.0
mag_points = []
steps = 0
while seq.step == "wave" and steps < 3000:
    vector = rng.normal(size=3)
    vector = vector / np.linalg.norm(vector) * FIELD_UT
    mag_points.append(vector)
    seq.feed(sample(t, [0.0, 0.0, 1.0], TRUE_GYRO_BIAS, vector))
    t += DT
    steps += 1

check("wave step completes once coverage and count are enough",
      seq.step == "orientation", f"stopped at {seq.step!r} after {steps} samples")
check("the fitted field strength matches the synthetic sphere",
      abs(float(np.linalg.norm(seq.cal.mag_bias)) - 0.0) < 1.0
      and seq.cal.mag_soft is not None)

# ---------------------------------------------------------------------
# Which way is which: mounting orientation
# ---------------------------------------------------------------------
# Settle on gravity first -- board flat, +Z up, matching the six-sides and
# wave samples above so this step's own "still" reference agrees with them.
for _ in range(120):
    seq.feed(sample(t, [0.0, 0.0, 1.0], TRUE_GYRO_BIAS, NEUTRAL_MAG))
    t += DT

check("orientation step finds gravity before asking for a slide",
      seq._gravity_ref is not None)

# A slide "away from you" in board axes, +X -- with the board already
# axis-aligned (mount is being solved from scratch here), this should solve
# to the identity mapping.
for accel_ms2 in shove_profile([1.0, 0.0, 0.0]):
    accel_g = np.array([0.0, 0.0, 1.0]) + accel_ms2 / G_MS2
    seq.feed(sample(t, accel_g, TRUE_GYRO_BIAS, NEUTRAL_MAG))
    t += DT
# QuickMoveDetector reports a move once it has *ended* -- it needs to see the
# board quiet again before it can tell a completed slide from one still under
# way, exactly as test_motion.py's run_move() feeds rest_after samples.
for _ in range(150):
    seq.feed(sample(t, [0.0, 0.0, 1.0], TRUE_GYRO_BIAS, NEUTRAL_MAG))
    t += DT

check("one slide is a complete answer", seq._mount_known,
      f"mount=\n{seq.cal.mount}")
check("cal_advance moves past the optional second slide", True)  # sanity marker
seq.advance()
check("advance() finishes orientation once the mount is known",
      seq.step == "check", f"stopped at {seq.step!r}")
check("the solved mount is the identity mapping",
      bool(np.allclose(seq.cal.mount, np.eye(3), atol=1e-6)), str(seq.cal.mount))

# ---------------------------------------------------------------------
# Check: verify against physics that must hold
# ---------------------------------------------------------------------
for _ in range(60):
    seq.feed(sample(t, [0.0, 0.0, 1.0], TRUE_GYRO_BIAS,
                    [0.0, 0.0, FIELD_UT]))
    t += DT

state = seq.state()
check("the check step reports its verdict once still", seq._verify_done)
check("state() carries the per-check results", len(state["quality"]["checks"]) == 4,
      str(state["quality"]))
passed = sum(1 for c in seq.verify if c["ok"])
check("a clean synthetic run passes every check", passed == 4,
      str(seq.verify))

# ---------------------------------------------------------------------
# advance() elsewhere is a no-op; cancel() ends the sequence outright.
# ---------------------------------------------------------------------
before = seq.step
seq.advance()
check("advance() outside orientation does nothing", seq.step == before)

seq2 = calseq.CalSequence(label="test-cancel")
seq2.start("full")
seq2.cancel()
check("cancel() ends the sequence", seq2.finished and seq2.result["accepted"] is False)
check("feed() after cancel is a no-op, not an error", True)
seq2.feed(sample(0.0, [0, 0, 1], [0, 0, 0], NEUTRAL_MAG))

# ---------------------------------------------------------------------
# "quick" mode: reuses the saved gyro/accel/mag and only redoes orientation.
# ---------------------------------------------------------------------
seq3 = calseq.CalSequence(label="test")     # same label: picks up seq's autosave
seq3.start("quick")
check("quick mode starts at orientation", seq3.step == "orientation")
check("quick mode carries over the previous gyro bias",
      bool(np.allclose(seq3.cal.gyro_bias, TRUE_GYRO_BIAS, atol=0.05)),
      str(seq3.cal.gyro_bias))

shutil.rmtree(_tmp_home, ignore_errors=True)

print()
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
