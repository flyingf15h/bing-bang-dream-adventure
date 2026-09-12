"""Self-checks for the fusion and calibration maths.

Runs without a display or hardware:  python tests/test_math.py
"""

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bbda.fusion import MadgwickAHRS, quat_to_matrix, tilt_from_accel
from bbda.calibration import fit_ellipsoid, AccelSixPointCollector, SIX_POSITIONS, Calibration

fail = 0
def check(name, cond, extra=""):
    global fail
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond: fail += 1

# ---------------- Ellipsoid fit ----------------
rng = np.random.default_rng(7)
true_centre = np.array([12.0, -30.0, 8.5])
# soft-iron distortion: non-uniform scaling + rotation
axes = np.array([1.30, 0.85, 1.05])
th = 0.4
R = np.array([[math.cos(th), -math.sin(th), 0], [math.sin(th), math.cos(th), 0], [0, 0, 1]])
F = 48.0  # uT field

v = rng.normal(size=(4000, 3))
v /= np.linalg.norm(v, axis=1, keepdims=True)
ideal = v * F
distorted = (R @ np.diag(axes) @ R.T @ ideal.T).T + true_centre

res = fit_ellipsoid(distorted)
check("mag fit ok", res.ok, res.message)
check("mag fit centre", np.allclose(res.bias, true_centre, atol=0.5), f"{res.bias}")
F_geo = F * float(np.prod(axes) ** (1/3))
check("mag fit field", abs(res.field_strength - F_geo) < 1.0, f"{res.field_strength:.2f} vs geo-mean {F_geo:.2f}")
corrected = (res.soft @ (distorted - res.bias).T).T
n = np.linalg.norm(corrected, axis=1)
check("mag fit sphericity", n.std() / n.mean() < 0.01, f"residual {res.residual_pct:.3f}%")
check("mag fit radius==field", abs(n.mean() - F_geo) < 1.0, f"{n.mean():.2f}")

# noisy version
noisy = distorted + rng.normal(scale=0.8, size=distorted.shape)
res2 = fit_ellipsoid(noisy)
check("mag fit with noise", res2.ok and np.allclose(res2.bias, true_centre, atol=1.5), f"{res2.bias}")

# degenerate (coplanar) input should fail gracefully
flat = ideal.copy(); flat[:, 2] = 0
res3 = fit_ellipsoid(flat + true_centre)
check("mag fit rejects coplanar", not res3.ok, res3.message)
check("mag fit rejects tiny input", not fit_ellipsoid(np.zeros((5, 3))).ok)

# ---------------- Six-point accel ----------------
true_bias = np.array([0.02, -0.01, 0.03])
true_gain = np.array([0.98, 1.03, 0.99])
col = AccelSixPointCollector(samples_per_position=10)
for name, ax, sign, _ in SIX_POSITIONS:
    ideal_a = np.zeros(3); ideal_a[{"x":0,"y":1,"z":2}[ax]] = sign
    raw = ideal_a / true_gain + true_bias   # inverse of the correction model
    col.start(name)
    for _ in range(10):
        col.add(raw + rng.normal(scale=1e-4, size=3))
check("accel six-point complete", col.complete)
b, s = col.result()
check("accel bias recovered", np.allclose(b, true_bias, atol=1e-3), f"{b}")
check("accel scale recovered", np.allclose(s, true_gain, atol=2e-3), f"{s}")

cal = Calibration(accel_bias=b, accel_scale=s)
out = cal.apply_accel(np.array([1.0]) / true_gain[0] * np.array([1,0,0]) + true_bias)
check("accel correction round-trip", np.allclose(out, [1, 0, 0], atol=2e-3), f"{out}")

# ---------------- Madgwick ----------------
# Level board, x axis pointing at magnetic north, 60 deg dip.
dip = math.radians(60)
mag_world = np.array([math.cos(dip), 0.0, -math.sin(dip)]) * 48.0
acc_level = np.array([0.0, 0.0, 1.0])

f = MadgwickAHRS(beta=0.5)
for _ in range(4000):
    f.update(np.zeros(3), acc_level, mag_world, 0.005)
r, p, y = f.euler_degrees()
check("madgwick level converges", abs(r) < 0.5 and abs(p) < 0.5 and abs(y) < 0.5, f"rpy=({r:.3f},{p:.3f},{y:.3f})")
check("madgwick heading north", f.heading_degrees() < 0.5 or f.heading_degrees() > 359.5, f"{f.heading_degrees():.2f}")

# Rotate the board 30 deg roll: world->body maps gravity/mag into body axes.
def rot_x(a):
    return np.array([[1,0,0],[0,math.cos(a),-math.sin(a)],[0,math.sin(a),math.cos(a)]])
def rot_z(a):
    return np.array([[math.cos(a),-math.sin(a),0],[math.sin(a),math.cos(a),0],[0,0,1]])

for label, Rwb, expect in [
    ("roll 30", rot_x(math.radians(30)), (30.0, 0.0, 0.0)),
    ("yaw 90",  rot_z(math.radians(90)), (0.0, 0.0, 90.0)),
]:
    body_acc = Rwb.T @ np.array([0, 0, 1.0])
    body_mag = Rwb.T @ mag_world
    f2 = MadgwickAHRS(beta=0.5)
    for _ in range(6000):
        f2.update(np.zeros(3), body_acc, body_mag, 0.005)
    got = f2.euler_degrees()
    check(f"madgwick {label}", all(abs(a - b) < 1.0 for a, b in zip(got, expect)),
          f"got=({got[0]:.2f},{got[1]:.2f},{got[2]:.2f}) want={expect}")
    Rm = f2.rotation_matrix()
    check(f"madgwick {label} matrix", np.allclose(Rm, Rwb, atol=0.03))

# heading for yaw 90 (board x pointing west) should read 270 compass
f3 = MadgwickAHRS(beta=0.5)
Rwb = rot_z(math.radians(90))
for _ in range(6000):
    f3.update(np.zeros(3), Rwb.T @ np.array([0,0,1.0]), Rwb.T @ mag_world, 0.005)
check("madgwick compass heading", abs(f3.heading_degrees() - 270.0) < 1.0, f"{f3.heading_degrees():.2f}")

# 6-axis fallback (no mag) still gets roll/pitch
f4 = MadgwickAHRS(beta=0.5)
Rwb = rot_x(math.radians(-25))
for _ in range(6000):
    f4.update(np.zeros(3), Rwb.T @ np.array([0,0,1.0]), None, 0.005)
r4, p4, _ = f4.euler_degrees()
check("madgwick 6-axis roll", abs(r4 + 25.0) < 1.0 and abs(p4) < 1.0, f"roll={r4:.2f} pitch={p4:.2f}")

# gyro-only integration: 90 deg/s about z for 1 s -> 90 deg yaw
f5 = MadgwickAHRS(beta=0.0)
for _ in range(1000):
    f5.update(np.array([0.0, 0.0, 90.0]), np.zeros(3), None, 0.001)
_, _, y5 = f5.euler_degrees()
check("gyro integration", abs(y5 - 90.0) < 0.5, f"yaw={y5:.3f}")

# accel-only tilt helper agrees with the filter
tr, tp = tilt_from_accel(rot_x(math.radians(-25)).T @ np.array([0,0,1.0]))
check("tilt_from_accel", abs(tr + 25) < 0.01 and abs(tp) < 0.01, f"({tr:.3f},{tp:.3f})")

# dt guards
f6 = MadgwickAHRS()
q_before = f6.q.copy()
f6.update(np.array([100.0,0,0]), acc_level, mag_world, 0.0)
f6.update(np.array([100.0,0,0]), acc_level, mag_world, 5.0)
check("dt guard", np.allclose(f6.q, q_before))

print()
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
