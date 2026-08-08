"""Translation estimation and gesture detection.

What lives here:

* :class:`DeadReckoning` -- the simple estimator: position by double-
  integrating the accelerometer after removing gravity in the world frame,
  with a hard zero-velocity clamp.
* :class:`KalmanDeadReckoning` -- the default estimator: the same physics run
  through a 9-state error-state Kalman filter, which is markedly more accurate
  for the reasons set out below.
* :class:`SectorMap` -- quantises a direction in a plane into an arbitrary
  number of named sectors, shared by both direction-aware detectors.
* :class:`FlickFrame` -- which way the board points, which is what turns a
  rotation into the direction a person would say the flick went.
* :class:`FlickDetector` -- impulsive rotations ("flicks"), reporting which
  board axis was flicked, which of N sectors it fell in, or how many degrees
  clockwise from straight up the flick actually went.
* :class:`QuickMoveDetector` -- a quick *translation* in one direction: a
  shove, swipe or lift, reported the same way.
* :class:`FlipDetector` -- sustained orientation changes, reporting which board
  face ended up pointing at the sky.

A warning about position, stated plainly because the UI depends on it
--------------------------------------------------------------------
Position from an IMU alone is dead reckoning, and dead reckoning with a
consumer MEMS part diverges fast. Accelerometer bias is integrated twice, so a
residual of 10 mg -- well within this part's spec -- becomes roughly 0.05 m of
error after 1 second and 5 m after 10 seconds, growing with the square of
time. Nothing here fixes that; there is no GPS, no camera and no wheel encoder
to bound it. Both estimators are honest about it and neither should be read as
a position fix.

Why there are two estimators
----------------------------
:class:`DeadReckoning` is the straightforward version and is kept because it
is easy to reason about and to check against. It has three weaknesses that are
inherent to its structure rather than to its tuning:

1. it integrates each step as a rectangle, which leaves a per-step velocity
   error proportional to how much the acceleration changed over that step;
2. its zero-velocity update *discards* the velocity error instead of learning
   from it -- the accumulated position error stays in the estimate forever;
3. its bias tracker is a fixed-gain leak with no notion of how well the bias
   is currently known, so it is either too slow to converge or too eager to
   absorb real motion.

Of the three, the second is by far the most important. That is worth saying
plainly because the first is the one that looks most like a bug and is
actually the least significant: at 200 Hz, on a symmetric push-and-stop, the
two integration schemes land within a few microns of each other, and the
published comparisons agree that integration order only starts to matter as
the sample rate falls and the motion gets faster.

:class:`KalmanDeadReckoning` addresses all three with the standard techniques
from the ZUPT-aided inertial navigation literature, which is the approach
essentially every foot-mounted pedestrian navigation system uses:

* **Trapezoidal integration.** Velocity uses the mean of the current and
  previous acceleration, position the mean of the current and previous
  velocity. Where this earns its keep is not the symmetric case but the
  asymmetric one: it is *exact* when acceleration varies linearly across the
  step, whereas the rectangular form leaves a residue of about
  ``a * dt / 2`` per step that only cancels if the motion is symmetric. Push
  the board gently and stop it sharply and the rectangular error does not
  cancel -- measured on a constant-jerk ramp the two differ by three orders
  of magnitude, 4 micrometres against 5 millimetres over one second.
* **An error-state Kalman filter** over 9 states -- position, velocity and
  *body-frame* accelerometer bias -- rather than a fixed-gain correction. The
  covariance grows while moving and shrinks at each stop, so the filter knows
  how much to trust each new zero-velocity constraint instead of applying the
  same gain regardless.
* **ZUPT as a measurement, not a clamp.** Standing still is treated as an
  observation "velocity is zero, to within this much noise". Because the
  filter has been tracking the correlation between velocity error, position
  error and bias error, that one observation corrects all three: the position
  is pulled back along the direction the error is known to have accumulated,
  and the bias estimate improves. This is the single biggest difference. The
  clamp in the simple estimator throws exactly this information away.
* **Body-frame bias.** The bias belongs to the sensor, not to the world, so it
  is held in board axes and rotated into the world frame each step. A
  world-frame bias is only correct while the board keeps one attitude.
* **Retro-correction of the drawn path.** When a stop ends a moving segment,
  the position correction the filter just applied is spread back over that
  segment's stored points as a linear ramp, so the drawn trajectory agrees
  with the corrected endpoint instead of ending in a visible jump. This is a
  cheap stand-in for a proper backward (RTS) smoother.

Sources, all describing the same standard construction: the ZUPT-aided
INS/EKF formulation reviewed in "A Review on ZUPT-Aided Pedestrian Inertial
Navigation" and the INS-EKF-ZUPT ("IEZ") method it builds on; the error-state
formulation as used in mbrossar/ai-imu-dr; and the general result that
integration order matters most exactly where this application lives -- large
steps and fast motion (Sensors, "Effect of Strapdown Integration Order and
Sampling Rate on IMU-Based Attitude Estimation Accuracy").
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

G_MS2 = 9.80665

AXIS_NAMES = ("x", "y", "z")
FACE_LABELS = {
    (0, +1): "+X", (0, -1): "-X",
    (1, +1): "+Y", (1, -1): "-Y",
    (2, +1): "+Z", (2, -1): "-Z",
}

PLANE_NAMES = {(0, 1): "X-Y", (1, 2): "Y-Z", (0, 2): "X-Z"}


# ----------------------------------------------------------------------
# Stationary detection
# ----------------------------------------------------------------------
class StationaryDetector:
    """Declares the board still when both sensors agree it is.

    Requiring the gyroscope to agree is what stops a constant-velocity slide
    from being mistaken for rest -- though nothing can distinguish those two
    cases from accelerometer data alone, which is a property of the physics,
    not of this code.

    Two ways of judging it
    ----------------------
    **Absolute** (the default) asks that the accelerometer read 1 g and the
    gyroscope read zero, to within a tolerance, across the whole window. That
    is the right question to ask of *calibrated* readings, which is what the
    dead-reckoning ZUPT feeds it.

    **Bias-blind** (``bias_blind=True``) asks instead that the readings not be
    *changing*, and separately that they be somewhere sane. It exists because
    calibration is fed raw readings by definition -- it is measuring the
    offsets, so it cannot assume them away -- and a MEMS gyroscope with
    several degrees per second of zero-rate offset is entirely normal, as is
    an accelerometer reading 1.04 g flat on a table. Judged absolutely, such a
    board *never* reads still, so every capture that waits for stillness waits
    for ever and the calibration that would have removed the offset is the one
    thing that cannot run. How much the readings vary does not care what the
    offsets are, so that is what this mode measures; the sanity bands are
    there only to reject a board being carried at a steady rate, which
    genuinely does look like rest to a spread test.
    """

    def __init__(
        self,
        accel_tolerance_g: float = 0.04,
        gyro_tolerance_dps: float = 2.5,
        window: int = 20,
        bias_blind: bool = False,
        accel_sanity_g: float = 0.25,
        gyro_sanity_dps: float = 25.0,
    ) -> None:
        self.accel_tolerance_g = accel_tolerance_g
        self.gyro_tolerance_dps = gyro_tolerance_dps
        self.bias_blind = bias_blind
        self.accel_sanity_g = accel_sanity_g
        self.gyro_sanity_dps = gyro_sanity_dps
        self._accel = deque(maxlen=window)   # |accel|, g
        self._gyro = deque(maxlen=window)    # gyro vectors, dps
        self.stationary = True

    @property
    def ready(self) -> bool:
        """True once a full window has been seen.

        Before that ``stationary`` is only an assumption, and anything that
        acts on it -- learning a bias in particular -- must wait."""
        return len(self._accel) >= self._accel.maxlen

    def update(self, accel_g: np.ndarray, gyro_dps: np.ndarray) -> bool:
        self._accel.append(float(np.linalg.norm(accel_g)))
        self._gyro.append(np.asarray(gyro_dps, dtype=float).copy())
        if len(self._accel) < self._accel.maxlen:
            return self.stationary

        accel = np.array(self._accel)
        gyro = np.array(self._gyro)
        rates = np.linalg.norm(gyro, axis=1)

        if self.bias_blind:
            # Per axis for the gyroscope, not on the magnitude: an offset of
            # +5 dps on one axis and -5 on another leaves the magnitude
            # perfectly steady while the axes wander, and it is the axes the
            # callers of this go on to average.
            self.stationary = bool(
                float(accel.max() - accel.min()) < self.accel_tolerance_g
                and float(np.max(gyro.max(axis=0) - gyro.min(axis=0)))
                < self.gyro_tolerance_dps
                and abs(float(accel.mean()) - 1.0) < self.accel_sanity_g
                and float(rates.max()) < self.gyro_sanity_dps
            )
        else:
            self.stationary = bool(
                float(np.abs(accel - 1.0).max()) < self.accel_tolerance_g
                and float(rates.max()) < self.gyro_tolerance_dps
            )
        return self.stationary

    def reset(self) -> None:
        self._accel.clear()
        self._gyro.clear()
        self.stationary = True


# ----------------------------------------------------------------------
# Dead reckoning
# ----------------------------------------------------------------------
@dataclass
class MotionState:
    position: np.ndarray = field(default_factory=lambda: np.zeros(3))   # metres
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(3))   # m/s
    linear_accel: np.ndarray = field(default_factory=lambda: np.zeros(3))  # m/s^2, world
    speed: float = 0.0
    distance: float = 0.0        # path length travelled, metres
    stationary: bool = True
    seconds_moving: float = 0.0  # time since the last ZUPT, drives the drift estimate

    # Filled in by KalmanDeadReckoning only; the simple estimator has no
    # opinion on either and leaves them at their defaults.
    accel_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))  # m/s^2, body
    position_sigma: float = 0.0  # 1-sigma position uncertainty, metres

    @property
    def drift_estimate_m(self) -> float:
        """Order-of-magnitude position error, from 10 mg of residual bias.

        0.5 * a * t^2 with a = 0.01 g. Deliberately a rough number: it exists
        to stop anyone reading the position as trustworthy, not to be exact.

        :class:`KalmanDeadReckoning` overrides this with ``position_sigma``,
        which is the filter's own covariance rather than a rule of thumb.
        """
        return 0.5 * 0.01 * G_MS2 * self.seconds_moving ** 2


class DeadReckoning:
    """Position and velocity from world-frame acceleration."""

    def __init__(
        self,
        velocity_damping: float = 0.8,
        zupt_enabled: bool = True,
        bias_learn_rate: float = 0.02,
    ) -> None:
        self.velocity_damping = velocity_damping   # per second
        self.zupt_enabled = zupt_enabled
        self.bias_learn_rate = bias_learn_rate

        self.state = MotionState()
        self.detector = StationaryDetector()
        self._bias_world = np.zeros(3)   # residual world-frame accel, m/s^2
        self._path: list[np.ndarray] = [np.zeros(3)]

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.state = MotionState()
        self.detector.reset()
        self._bias_world = np.zeros(3)
        self._path = [np.zeros(3)]

    def reset_origin(self) -> None:
        """Keep the learned bias but move the origin to here."""
        self.state.position = np.zeros(3)
        self.state.velocity = np.zeros(3)
        self.state.distance = 0.0
        self.state.seconds_moving = 0.0
        self._path = [np.zeros(3)]

    @property
    def path(self) -> np.ndarray:
        return np.array(self._path) if self._path else np.zeros((1, 3))

    # ------------------------------------------------------------------
    def update(
        self,
        rotation: np.ndarray,
        accel_g: np.ndarray,
        gyro_dps: np.ndarray,
        dt: float,
    ) -> MotionState:
        if dt <= 0 or dt > 0.5:
            return self.state

        stationary = self.detector.update(accel_g, gyro_dps)
        self.state.stationary = stationary

        # Rotate into the world frame and take gravity out. Any error in the
        # orientation estimate leaks gravity into the horizontal axes here,
        # which is why a good accelerometer calibration matters so much more
        # for position than it does for attitude: a 1 degree tilt error is
        # 0.017 g of phantom horizontal acceleration.
        accel_world = rotation @ np.asarray(accel_g, dtype=float)
        linear = (accel_world - np.array([0.0, 0.0, 1.0])) * G_MS2
        linear = linear - self._bias_world
        self.state.linear_accel = linear

        if stationary:
            # Everything measured now is bias by definition -- but only once
            # the detector has a full window behind it. Until then "stationary"
            # is just its initial assumption, and if the board happens to be
            # moving when the link opens, learning from it poisons the bias
            # estimate for the rest of the session.
            if self.detector.ready:
                self._bias_world += self.bias_learn_rate * (linear - 0.0)
            if self.zupt_enabled:
                self.state.velocity = np.zeros(3)
                self.state.seconds_moving = 0.0
            self.state.speed = 0.0
            return self.state

        self.state.seconds_moving += dt
        self.state.velocity = self.state.velocity + linear * dt
        # Continuous leak toward zero, so one missed ZUPT does not run away.
        self.state.velocity *= max(0.0, 1.0 - self.velocity_damping * dt)

        step = self.state.velocity * dt
        self.state.position = self.state.position + step
        self.state.distance += float(np.linalg.norm(step))
        self.state.speed = float(np.linalg.norm(self.state.velocity))

        if np.linalg.norm(self.state.position - self._path[-1]) > 0.002:
            self._path.append(self.state.position.copy())
            if len(self._path) > 4000:
                self._path.pop(0)

        return self.state


# ----------------------------------------------------------------------
# Dead reckoning, error-state Kalman form
# ----------------------------------------------------------------------
class KalmanDeadReckoning:
    """Position and velocity from a 9-state error-state Kalman filter.

    The nominal state is position, velocity and body-frame accelerometer bias.
    The filter tracks the *errors* in those, which keeps the linearisation
    valid: errors stay small even when the state itself does not.

    Error dynamics, with ``R`` the board-to-world rotation::

        d(dp)/dt = dv
        d(dv)/dt = -R db + n_a        (bias is subtracted, hence the sign)
        d(db)/dt = n_b

    so the discrete transition over one step is::

        [ I   I dt   -R dt^2/2 ]
        [ 0   I      -R dt     ]
        [ 0   0       I        ]

    Standing still is a measurement ``H x = v = 0`` with noise ``zupt_noise``.
    Because the off-diagonal blocks of the covariance have been accumulating
    the correlation between velocity, position and bias error, that single
    measurement corrects all three at once -- which is the whole point, and
    what a plain velocity clamp cannot do.

    Parameters
    ----------
    accel_noise:
        Accelerometer white noise, m/s^2 per root-Hz. This is the velocity
        random walk and sets how fast the velocity uncertainty grows. The
        measured still-board noise on this board is about 0.0015 g, so 0.08 is
        deliberately a few times that -- the term also has to absorb the
        gravity leakage caused by attitude error, which dominates it.
    bias_noise:
        Accelerometer bias random walk, m/s^3 per root-Hz. How quickly the
        filter is willing to believe the bias has changed, mostly with
        temperature.
    zupt_noise:
        How exactly "stationary" means zero velocity, in m/s. Not zero: the
        stationary detector has a tolerance, so the board may genuinely be
        creeping slightly when it fires.
    """

    def __init__(
        self,
        accel_noise: float = 0.08,
        bias_noise: float = 0.002,
        zupt_noise: float = 0.01,
        zupt_enabled: bool = True,
        initial_bias_sigma: float = 0.2,
        retro_correct: bool = True,
    ) -> None:
        self.accel_noise = accel_noise
        self.bias_noise = bias_noise
        self.zupt_noise = zupt_noise
        self.zupt_enabled = zupt_enabled
        self.initial_bias_sigma = initial_bias_sigma
        self.retro_correct = retro_correct

        self.detector = StationaryDetector()
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.state = MotionState()
        self.detector.reset()
        self._bias = np.zeros(3)          # body frame, m/s^2
        self._prev_accel_world: np.ndarray | None = None
        self._path: list[np.ndarray] = [np.zeros(3)]
        self._segment_start = 0           # index in _path where this move began

        # Position and velocity start perfectly known -- they are *defined* as
        # zero here, that is what "origin" means. Only the bias is uncertain.
        self._P = np.zeros((9, 9))
        self._P[6:9, 6:9] = np.eye(3) * self.initial_bias_sigma ** 2

    def reset_origin(self) -> None:
        """Keep the learned bias and its covariance, move the origin to here."""
        self.state.position = np.zeros(3)
        self.state.velocity = np.zeros(3)
        self.state.distance = 0.0
        self.state.seconds_moving = 0.0
        self._path = [np.zeros(3)]
        self._segment_start = 0
        self._P[0:3, :] = 0.0
        self._P[:, 0:3] = 0.0

    @property
    def path(self) -> np.ndarray:
        return np.array(self._path) if self._path else np.zeros((1, 3))

    @property
    def bias(self) -> np.ndarray:
        """Current body-frame accelerometer bias estimate, m/s^2."""
        return self._bias.copy()

    # ------------------------------------------------------------------
    def update(
        self,
        rotation: np.ndarray,
        accel_g: np.ndarray,
        gyro_dps: np.ndarray,
        dt: float,
    ) -> MotionState:
        if dt <= 0 or dt > 0.5:
            return self.state

        stationary = self.detector.update(accel_g, gyro_dps)
        self.state.stationary = stationary

        rot = np.asarray(rotation, dtype=float)
        accel_body = np.asarray(accel_g, dtype=float) * G_MS2

        # --- nominal propagation, trapezoidal ---------------------------
        accel_world = rot @ (accel_body - self._bias) - np.array([0.0, 0.0, G_MS2])
        previous = self._prev_accel_world
        if previous is None:
            previous = accel_world
        mean_accel = 0.5 * (previous + accel_world)
        self._prev_accel_world = accel_world
        self.state.linear_accel = accel_world

        velocity_before = self.state.velocity
        velocity = velocity_before + mean_accel * dt
        step = 0.5 * (velocity_before + velocity) * dt

        self.state.velocity = velocity
        self.state.position = self.state.position + step
        self.state.distance += float(np.linalg.norm(step))
        self.state.speed = float(np.linalg.norm(velocity))

        # --- covariance propagation -------------------------------------
        self._propagate_covariance(rot, dt)

        if stationary:
            self.state.seconds_moving = 0.0
            if self.detector.ready and self.zupt_enabled:
                self._zupt()
        else:
            self.state.seconds_moving += dt
            if len(self._path) and np.linalg.norm(
                self.state.position - self._path[-1]
            ) > 0.002:
                self._append_path()

        self.state.accel_bias = self._bias.copy()
        self.state.position_sigma = float(
            np.sqrt(max(0.0, np.trace(self._P[0:3, 0:3])))
        )
        return self.state

    # ------------------------------------------------------------------
    def _propagate_covariance(self, rot: np.ndarray, dt: float) -> None:
        eye = np.eye(3)
        phi = np.eye(9)
        phi[0:3, 3:6] = eye * dt
        phi[0:3, 6:9] = -rot * (0.5 * dt * dt)
        phi[3:6, 6:9] = -rot * dt

        # Process noise for a double integrator driven by white acceleration,
        # plus a random walk on the bias. The position/velocity cross term is
        # kept because dropping it understates how correlated the two are,
        # which is exactly the correlation the ZUPT exploits.
        va = self.accel_noise ** 2
        vb = self.bias_noise ** 2
        q = np.zeros((9, 9))
        q[0:3, 0:3] = eye * (va * dt ** 3 / 3.0)
        q[0:3, 3:6] = eye * (va * dt ** 2 / 2.0)
        q[3:6, 0:3] = eye * (va * dt ** 2 / 2.0)
        q[3:6, 3:6] = eye * (va * dt)
        q[6:9, 6:9] = eye * (vb * dt)

        self._P = phi @ self._P @ phi.T + q

    def _zupt(self) -> None:
        """Apply the zero-velocity measurement and correct all three states."""
        innovation = -self.state.velocity          # measurement is v = 0
        s = self._P[3:6, 3:6] + np.eye(3) * self.zupt_noise ** 2
        try:
            gain = np.linalg.solve(s.T, self._P[:, 3:6].T).T   # P H^T S^-1
        except np.linalg.LinAlgError:
            return

        correction = gain @ innovation
        position_correction = correction[0:3]

        self.state.position = self.state.position + position_correction
        self.state.velocity = self.state.velocity + correction[3:6]
        self._bias = self._bias + correction[6:9]

        # Joseph form: stays symmetric and positive definite over long runs,
        # where the short (I - K H) P form slowly loses both.
        ikh = np.eye(9)
        ikh[:, 3:6] -= gain
        noise = np.eye(3) * self.zupt_noise ** 2
        self._P = ikh @ self._P @ ikh.T + gain @ noise @ gain.T
        self._P = 0.5 * (self._P + self._P.T)

        if self.retro_correct:
            self._retro_correct(position_correction)
        self._segment_start = len(self._path) - 1

    def _retro_correct(self, correction: np.ndarray) -> None:
        """Spread a position correction back over the segment that earned it.

        The filter has just decided the endpoint of this segment was wrong by
        ``correction``. That error did not appear at the last sample -- it
        accumulated across the whole segment, roughly linearly in the
        integrated velocity error. Ramping the correction over the stored
        points leaves the drawn path consistent with the corrected endpoint
        instead of ending in a step, which is what a backward smoother would
        do properly and this approximates for free.
        """
        last = len(self._path) - 1
        span = last - self._segment_start
        if span < 1:
            return
        for offset in range(1, span + 1):
            self._path[self._segment_start + offset] += correction * (offset / span)

    def _append_path(self) -> None:
        self._path.append(self.state.position.copy())
        if len(self._path) > 4000:
            self._path.pop(0)
            self._segment_start = max(0, self._segment_start - 1)


# ----------------------------------------------------------------------
# Direction sectors
# ----------------------------------------------------------------------
@dataclass
class Sector:
    """One of N equal angular sectors that a direction was quantised into."""

    index: int
    count: int
    label: str
    angle_deg: float     # the measured angle, 0..360
    centre_deg: float    # the centre of the sector it landed in
    margin: float        # 0 at a sector boundary, 1 at the centre

    def __str__(self) -> str:
        return self.label


class SectorMap:
    """Divides the circle into ``count`` equal named sectors.

    Sector 0 is *centred* on ``offset_deg`` rather than starting there, so the
    natural directions land in the middle of a sector instead of on the
    boundary between two. With four sectors and no offset that puts +X, +Y, -X
    and -Y at the centre of their own sector, which is what someone flicking
    along an axis expects.

    ``margin`` reports how far from a boundary the measurement landed, scaled
    so 1.0 is the exact centre and 0.0 is exactly on the edge. Detectors use it
    the same way :class:`FlickDetector` uses axis dominance: a direction that
    sits on a boundary is genuinely ambiguous, and refusing to name it beats
    naming it by a coin toss.

    The angles handed to :meth:`sector_of` are in whatever convention the
    caller measures them in; this class only divides a circle. Directions
    people make with their hands are measured clockwise from up, and
    :class:`FlickFrame` is what produces those.
    """

    def __init__(
        self,
        count: int = 4,
        offset_deg: float = 0.0,
        labels: list[str] | None = None,
    ) -> None:
        if count < 2:
            raise ValueError("a sector map needs at least two sectors")
        if labels is not None and len(labels) != count:
            raise ValueError(f"expected {count} labels, got {len(labels)}")
        self.count = int(count)
        self.offset_deg = float(offset_deg)
        self.labels = list(labels) if labels else self._default_labels()

    def _default_labels(self) -> list[str]:
        """Name each sector by its centre angle, which is always unambiguous."""
        return [f"{self.centre_of(i):.0f}°" for i in range(self.count)]

    @property
    def width_deg(self) -> float:
        return 360.0 / self.count

    def centre_of(self, index: int) -> float:
        return (self.offset_deg + index * self.width_deg) % 360.0

    def sector_of(self, angle_deg: float) -> Sector:
        angle = float(angle_deg) % 360.0
        relative = (angle - self.offset_deg + 0.5 * self.width_deg) % 360.0
        index = int(relative // self.width_deg) % self.count
        # Distance from the sector centre, 0 .. half a sector.
        from_centre = abs(relative - (index + 0.5) * self.width_deg)
        return Sector(
            index=index,
            count=self.count,
            label=self.labels[index],
            angle_deg=angle,
            centre_deg=self.centre_of(index),
            margin=float(max(0.0, 1.0 - from_centre / (0.5 * self.width_deg))),
        )

    def sector_of_vector(self, vector: np.ndarray, plane: tuple[int, int]) -> Sector:
        """Quantise a 3D vector by the angle of its projection onto ``plane``.

        The angle is measured from the plane's first axis towards its second.
        """
        first, second = plane
        angle = math.degrees(math.atan2(float(vector[second]), float(vector[first])))
        return self.sector_of(angle)


def planarity(vector: np.ndarray, plane: tuple[int, int]) -> float:
    """Fraction of a vector's length lying in ``plane``, 0..1.

    The complement of this is how much of the motion pointed out of the plane,
    which is what makes a "sideways" answer meaningless -- so both direction
    detectors require it to be high before they name a sector.
    """
    v = np.asarray(vector, dtype=float)
    norm = float(np.linalg.norm(v))
    if norm < 1e-12:
        return 0.0
    return float(math.hypot(v[plane[0]], v[plane[1]]) / norm)


# ----------------------------------------------------------------------
# Naming a flick by where it went rather than by what it turned about
# ----------------------------------------------------------------------
AXIS_VECTORS = {
    "+X": np.array([1.0, 0.0, 0.0]), "-X": np.array([-1.0, 0.0, 0.0]),
    "+Y": np.array([0.0, 1.0, 0.0]), "-Y": np.array([0.0, -1.0, 0.0]),
    "+Z": np.array([0.0, 0.0, 1.0]), "-Z": np.array([0.0, 0.0, -1.0]),
}


@dataclass(frozen=True)
class FlickFrame:
    """Which way the board points, so a flick can be named as a direction.

    ``front`` is the axis pointing away from the person holding it and
    ``up`` the board's own up; both are unit vectors in the frame the detector
    is fed, which is the mount-corrected one. Everything else follows from
    those two.

    Why a frame is needed at all
    ----------------------------
    A gyroscope reports the axis a rotation happened *about*, and that is at
    right angles to the direction the person who made it would name. Flick the
    board's front upwards -- a pitch -- and the measured rotation is about the
    left-right axis. Quantise the rotation vector directly and the answer comes
    out a quarter turn from the truth: the pitch that a player calls "up" gets
    named after the sideways axis, and the *roll* about the front, which is not
    a direction at all, gets named "up" and "down" instead.

    So the direction reported here is where the front actually went, which is
    its velocity under that rotation, ``v = omega x front``. Up and down are
    then pitches, left and right are yaws, and a roll about the front sends the
    front nowhere at all -- ``v`` comes out zero-length, and
    :meth:`swing_fraction` is how the detector refuses it rather than naming
    it after rounding error.

    Which axis is the front is not something this can guess. Run easy mode's
    orientation step and the board's axes are rotated into forward / left / up,
    making ``+X`` right; without it the frame is whatever the silkscreen says,
    and picking the wrong axis is exactly what makes rolls read as up and down.
    """

    front: np.ndarray
    up: np.ndarray

    @property
    def left(self) -> np.ndarray:
        """The remaining axis, fixed by the other two being right-handed."""
        return np.cross(self.up, self.front)

    def sweep(self, rotation: np.ndarray) -> np.ndarray:
        """Velocity of the board's front under ``rotation``.

        Perpendicular to the front by construction, so it always lies in the
        plane the front sweeps through, and its length is the part of the
        rotation that is a swing rather than a twist.
        """
        return np.cross(np.asarray(rotation, dtype=float), self.front)

    def bearing_deg(self, rotation: np.ndarray) -> float:
        """Where the flick went, in degrees clockwise from up.

        0 up, 90 right, 180 down, 270 left -- the way a clock face is read,
        which is the way people describe a direction they made with a hand.
        """
        swept = self.sweep(rotation)
        return math.degrees(math.atan2(
            -float(np.dot(swept, self.left)), float(np.dot(swept, self.up))
        )) % 360.0

    def swing_fraction(self, rotation: np.ndarray) -> float:
        """How much of ``rotation`` swung the front rather than twisting it.

        1 for a pure pitch or yaw, 0 for a pure roll about the front. A roll
        has no direction to report -- it leaves the front pointing exactly
        where it was -- so a floor on this is what keeps one from being
        reported as a flick upwards.
        """
        length = float(np.linalg.norm(np.asarray(rotation, dtype=float)))
        if length < 1e-12:
            return 0.0
        return float(np.linalg.norm(self.sweep(rotation)) / length)


#: Front axes offered in the UI, with the board's own up for each. Up is the
#: vertical axis except where that is the front itself, in which case the
#: board is pointing at or away from the viewer and its forward axis is what
#: reads as up on its face.
FLICK_FRONT_CHOICES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


def flick_frame(front: str = "+X") -> FlickFrame:
    """The frame for a named front axis, with a sensible up chosen for it."""
    if front not in AXIS_VECTORS:
        raise ValueError(f"unknown front axis {front!r}")
    up = "+X" if front in ("+Z", "-Z") else "+Z"
    return FlickFrame(front=AXIS_VECTORS[front], up=AXIS_VECTORS[up])


def flick_bearing_map(count: int = 6, offset_deg: float = 0.0) -> SectorMap:
    """Sectors named by their angle clockwise from straight up.

    Six sectors gives the set the game asks for: 0 for a flick straight up,
    60 up-and-right, 120 down-and-right, 180 straight down, 240 down-and-left
    and 300 up-and-left.
    """
    return SectorMap(count, offset_deg)


# ----------------------------------------------------------------------
# Flick detection
# ----------------------------------------------------------------------
@dataclass
class Flick:
    t: float
    axis: str            # "x", "y" or "z"
    direction: int       # +1 or -1, right-hand rule about that axis
    peak_dps: float      # peak angular rate on the dominant axis
    peak_accel_g: float  # peak linear acceleration during the event
    dominance: float     # 0..1, how single-axis the rotation was
    duration_ms: float
    sector: Sector | None = None   # set only in sector mode
    peak_vector: np.ndarray = field(default_factory=lambda: np.zeros(3))

    @property
    def label(self) -> str:
        if self.sector is not None:
            return self.sector.label
        return f"{'+' if self.direction > 0 else '-'}{self.axis.upper()}"


class FlickDetector:
    """Detects a short, sharp rotation and names the axis it happened about.

    A flick is an impulse: angular rate rises past a threshold, peaks, and
    falls back within a few hundred milliseconds. The detector is a small
    state machine rather than a simple threshold so that it can report the
    *peak* of the whole event -- a plain threshold would fire on the leading
    edge, where the axis split is still ambiguous.

    ``dominance`` is the fraction of the peak rotation vector's magnitude that
    lies on the winning axis. Requiring it to be high is what makes "which
    axis" a meaningful answer instead of a coin toss on a diagonal flick: an
    exact 45-degree diagonal scores 0.707, so the default floor of 0.8 sits
    deliberately above that and rejects it rather than naming an axis it
    cannot really distinguish.

    Three ways of naming the direction
    ----------------------------------
    **Axis mode** (``sector_map=None``, the default) reports one of six
    answers: the board axis nearest the rotation, and its sign.

    **Sector mode** (``sector_map=SectorMap(6)``) projects the peak rotation
    onto a plane of two board axes and reports which of N equal sectors it
    fell in, for any N. Six sectors of 60 degrees is a good compromise between
    resolution and reliability; four is the same set of directions axis mode
    offers within one plane, and twelve is about as fine as a hand-thrown
    flick can be told apart.

    **Bearing mode** (``sector_map=flick_bearing_map(6), frame=flick_frame()``)
    is sector mode over the direction the flick *went* rather than the axis it
    turned about -- see :class:`FlickFrame`. Up and down are pitches, left and
    right are yaws, and a roll about the front is refused because it is not a
    direction. It is the mode to use when the answer is shown to a person or
    fed to a game, because "60 degrees" then means a flick up and to the right,
    which is what the person who threw it thinks they did. The other two modes
    report what the sensor saw, which is the right answer for checking the
    board and the wrong one for playing.

    All three reject what they cannot distinguish rather than guessing.
    ``min_dominance`` does the first half of that in each: the fraction on the
    winning axis in axis mode, the fraction lying in the divided plane in
    sector mode, and the fraction that swung the front rather than twisting it
    in bearing mode. ``min_margin`` does the second half in both sector modes,
    rejecting a flick that landed too near the boundary between two sectors.
    The narrower the sectors, the more often that fires -- which is the real
    cost of asking for more directions.
    """

    def __init__(
        self,
        on_threshold_dps: float = 150.0,
        off_threshold_dps: float = 40.0,
        min_duration_ms: float = 15.0,
        max_duration_ms: float = 700.0,
        refractory_ms: float = 250.0,
        min_dominance: float = 0.8,
        sector_map: SectorMap | None = None,
        plane: tuple[int, int] = (0, 1),
        min_margin: float = 0.25,
        frame: FlickFrame | None = None,
    ) -> None:
        self.on_threshold_dps = on_threshold_dps
        self.off_threshold_dps = off_threshold_dps
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.refractory_ms = refractory_ms
        self.min_dominance = min_dominance
        self.sector_map = sector_map
        self.plane = plane
        self.min_margin = min_margin
        self.frame = frame

        self._active = False
        self._start_t = 0.0
        self._peak_vector = np.zeros(3)
        self._peak_norm = 0.0
        self._peak_accel = 0.0
        self._last_emit_t = -1e9

    def reset(self) -> None:
        self._active = False
        self._peak_vector = np.zeros(3)
        self._peak_norm = 0.0
        self._peak_accel = 0.0
        self._last_emit_t = -1e9

    def update(self, t: float, gyro_dps: np.ndarray, accel_g: np.ndarray) -> Flick | None:
        gyro = np.asarray(gyro_dps, dtype=float)
        rate = float(np.linalg.norm(gyro))
        # Linear acceleration magnitude, gravity roughly removed. Body frame is
        # good enough here: this only feeds the reported strength, not the
        # detection itself.
        linear = abs(float(np.linalg.norm(accel_g)) - 1.0)

        if not self._active:
            if rate >= self.on_threshold_dps and (t - self._last_emit_t) * 1000.0 >= self.refractory_ms:
                self._active = True
                self._start_t = t
                self._peak_vector = gyro.copy()
                self._peak_norm = rate
                self._peak_accel = linear
            return None

        # Event in progress: track the peak.
        if rate > self._peak_norm:
            self._peak_norm = rate
            self._peak_vector = gyro.copy()
        self._peak_accel = max(self._peak_accel, linear)

        duration_ms = (t - self._start_t) * 1000.0
        if rate > self.off_threshold_dps and duration_ms <= self.max_duration_ms:
            return None

        # Event finished (or ran too long to be a flick).
        self._active = False
        self._last_emit_t = t

        if duration_ms < self.min_duration_ms or duration_ms > self.max_duration_ms:
            return None
        if self._peak_norm <= 0:
            return None

        index = int(np.argmax(np.abs(self._peak_vector)))
        axis_dominance = abs(self._peak_vector[index]) / self._peak_norm

        sector = None
        if self.sector_map is not None and self.frame is not None:
            # Bearing mode: a roll about the front is refused here, since it
            # leaves the front pointing where it was and so has no direction.
            swing = self.frame.swing_fraction(self._peak_vector)
            if swing < self.min_dominance:
                return None
            sector = self.sector_map.sector_of(
                self.frame.bearing_deg(self._peak_vector)
            )
            if sector.margin < self.min_margin:
                return None
            dominance = swing
        elif self.sector_map is not None:
            # In sector mode "dominance" means how much of the rotation lay in
            # the plane being divided up, not how much lay on one axis.
            in_plane = planarity(self._peak_vector, self.plane)
            if in_plane < self.min_dominance:
                return None
            sector = self.sector_map.sector_of_vector(self._peak_vector, self.plane)
            if sector.margin < self.min_margin:
                return None
            dominance = in_plane
        else:
            if axis_dominance < self.min_dominance:
                return None
            dominance = axis_dominance

        return Flick(
            t=t,
            axis=AXIS_NAMES[index],
            direction=1 if self._peak_vector[index] > 0 else -1,
            peak_dps=abs(float(self._peak_vector[index])),
            peak_accel_g=self._peak_accel,
            dominance=dominance,
            duration_ms=duration_ms,
            sector=sector,
            peak_vector=self._peak_vector.copy(),
        )


# ----------------------------------------------------------------------
# Flip detection
# ----------------------------------------------------------------------
@dataclass
class Flip:
    t: float
    from_face: str   # e.g. "+Z"
    to_face: str     # e.g. "-Z"
    axis: str
    direction: int


class FlipDetector:
    """Reports when the board settles with a different face pointing up.

    Unlike a flick this is a state change, not an impulse: it only fires once
    the board is still again, so turning it over slowly is detected just as
    reliably as throwing it over.
    """

    def __init__(self, settle_samples: int = 25, tolerance_g: float = 0.35) -> None:
        self.settle_samples = settle_samples
        self.tolerance_g = tolerance_g
        self._candidate: str | None = None
        self._count = 0
        self.current_face: str | None = None

    def reset(self) -> None:
        self._candidate = None
        self._count = 0
        self.current_face = None

    @staticmethod
    def face_of(accel_g: np.ndarray) -> tuple[str, int, int] | None:
        """Which board face points up, from gravity alone."""
        accel = np.asarray(accel_g, dtype=float)
        norm = float(np.linalg.norm(accel))
        if not 0.75 < norm < 1.25:
            return None      # accelerating too hard to read gravity
        index = int(np.argmax(np.abs(accel)))
        sign = 1 if accel[index] > 0 else -1
        if abs(accel[index]) / norm < 0.8:
            return None      # board is on a corner, no clear face
        return FACE_LABELS[(index, sign)], index, sign

    def update(self, t: float, accel_g: np.ndarray, gyro_dps: np.ndarray) -> Flip | None:
        if float(np.linalg.norm(gyro_dps)) > 25.0:
            self._count = 0
            return None

        found = self.face_of(accel_g)
        if found is None:
            self._count = 0
            return None
        face, index, sign = found

        if face != self._candidate:
            self._candidate = face
            self._count = 1
            return None

        self._count += 1
        if self._count < self.settle_samples:
            return None
        self._count = self.settle_samples   # stop counting once settled

        if self.current_face is None:
            self.current_face = face
            return None
        if face == self.current_face:
            return None

        previous = self.current_face
        self.current_face = face
        return Flip(
            t=t,
            from_face=previous,
            to_face=face,
            axis=AXIS_NAMES[index],
            direction=sign,
        )


# ----------------------------------------------------------------------
# Quick linear movement
# ----------------------------------------------------------------------
# Names for the sectors of the X-Y plane once the board's frame is known --
# X forward, Y left, Z up, which is what the easy-mode orientation step
# establishes. Any other count falls back to naming sectors by their centre
# angle, which is always correct if less friendly.
FRAME_SECTOR_LABELS = {
    3: ["forward", "back-left", "back-right"],
    4: ["forward", "left", "back", "right"],
    6: ["forward", "forward-left", "back-left", "back", "back-right",
        "forward-right"],
    8: ["forward", "forward-left", "left", "back-left", "back", "back-right",
        "right", "forward-right"],
}


def frame_sector_map(count: int, offset_deg: float = 0.0) -> SectorMap:
    """A :class:`SectorMap` labelled in words when the count allows it."""
    labels = FRAME_SECTOR_LABELS.get(count) if offset_deg == 0.0 else None
    return SectorMap(count, offset_deg, labels)


@dataclass
class QuickMove:
    """A short, deliberate translation in one direction."""

    t: float
    label: str
    direction: np.ndarray        # unit vector in the frame it was fed
    sector: Sector | None        # None for a vertical move
    vertical: int                # +1 up, -1 down, 0 in-plane
    peak_speed: float            # m/s
    distance: float              # m, path length over the event
    peak_accel: float            # m/s^2
    duration_ms: float
    planarity: float             # how much of the move lay in the plane


class QuickMoveDetector:
    """Detects a quick movement in one direction and names that direction.

    This is the translation counterpart of :class:`FlickDetector`: where a
    flick is a sharp *rotation*, a quick move is a sharp *shove* -- sliding the
    board forward across a desk, lifting it, swiping it left.

    Direction comes from the peak of the integrated velocity, not from the
    acceleration
    ------------------------------------------------------------------------
    This matters, and it is the one non-obvious decision here. A deliberate
    move starts at rest and ends at rest, so the acceleration is a push
    followed by an equal and opposite brake, and its integral over the whole
    event is zero. Reading the direction from the net acceleration would
    therefore give noise, and reading it from the instantaneous peak would
    give whichever of the two phases happened to be sharper -- so a hard stop
    would report the move as having gone *backwards*.

    Velocity does not have that problem: it rises during the push, peaks
    mid-move, and falls during the brake. Its peak points the way the board
    actually travelled, and its magnitude is a real speed rather than an
    arbitrary threshold count. Integrating |v| over the event gives the
    distance covered, which is worth reporting because it separates a nudge
    from a proper swipe.

    The velocity here is integrated *only across the event*, starting from
    zero, so unlike the dead-reckoned position it does not accumulate error
    between events. That is what makes it usable as a gesture even though
    position over minutes is not.

    Feed it world-frame linear acceleration with gravity already removed --
    ``MotionState.linear_accel`` from either estimator is exactly that. Feeding
    a board-frame vector works too and simply names directions in board axes
    instead.
    """

    def __init__(
        self,
        on_threshold_ms2: float = 2.5,
        off_threshold_ms2: float = 0.8,
        quiet_ms: float = 120.0,
        rearm_ms: float = 150.0,
        min_duration_ms: float = 60.0,
        max_duration_ms: float = 1500.0,
        refractory_ms: float = 250.0,
        min_speed: float = 0.12,
        min_distance: float = 0.02,
        sector_map: SectorMap | None = None,
        plane: tuple[int, int] = (0, 1),
        min_planarity: float = 0.7,
        min_margin: float = 0.25,
        vertical_fraction: float = 0.8,
        vertical_labels: tuple[str, str] = ("up", "down"),
    ) -> None:
        self.on_threshold_ms2 = on_threshold_ms2
        self.off_threshold_ms2 = off_threshold_ms2
        self.quiet_ms = quiet_ms
        self.rearm_ms = rearm_ms
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.refractory_ms = refractory_ms
        self.min_speed = min_speed
        self.min_distance = min_distance
        # As with FlickDetector, no sector map means axis mode: report the
        # nearest board axis and its sign, and accept any direction. That is
        # what the easy-mode orientation step needs, since it is trying to
        # *discover* the frame rather than name a direction within a known one.
        self.sector_map = sector_map
        self.plane = plane
        self.min_planarity = min_planarity
        self.min_margin = min_margin
        self.vertical_fraction = vertical_fraction
        self.vertical_labels = vertical_labels

        self.reset()

    @property
    def normal_axis(self) -> int:
        """The axis the plane does not contain -- 'up' for the default plane."""
        return ({0, 1, 2} - set(self.plane)).pop()

    @property
    def active(self) -> bool:
        """True while an event is being captured.

        Exposed so a caller can measure something *about* the movement -- how
        much the board turned during it, say -- over exactly the window the
        detector considers the movement to be, rather than over some window of
        its own that also covers the hand reaching for the board.
        """
        return self._active

    def reset(self) -> None:
        self._active = False
        self._start_t = 0.0
        self._velocity = np.zeros(3)
        self._prev_accel = np.zeros(3)
        self._peak_velocity = np.zeros(3)
        self._peak_speed = 0.0
        self._peak_accel = 0.0
        self._distance = 0.0
        self._quiet_ms = 0.0
        self._rest_ms = 0.0
        self._rested = False
        self._last_emit_t = -1e9

    # ------------------------------------------------------------------
    def update(self, t: float, linear_accel: np.ndarray, dt: float) -> QuickMove | None:
        accel = np.asarray(linear_accel, dtype=float)
        magnitude = float(np.linalg.norm(accel))

        if dt <= 0 or dt > 0.5:
            self._prev_accel = accel
            return None

        if not self._active:
            # The board has to be at rest before a move can start, and this is
            # not a nicety -- reading the direction from the peak of the
            # integrated velocity is only meaningful if that integration
            # started from a known velocity, and rest is the only velocity
            # this can know. Without the gate, a move beginning while the
            # previous one was still being suppressed would be picked up
            # halfway through, during its braking phase, and reported as
            # having gone the opposite way. That failure is silent and looks
            # exactly like a correct answer, which is what makes it worth a
            # guard rather than a comment.
            #
            # The "has rested" flag latches rather than being re-tested at the
            # trigger. Acceleration climbing towards the trigger has to pass
            # through the quiet threshold on the way, so a test evaluated at
            # the trigger would find the rest counter already zeroed by the
            # first sample of the very move it is meant to admit, and nothing
            # would ever fire.
            if magnitude < self.off_threshold_ms2:
                self._rest_ms += dt * 1000.0
                if self._rest_ms >= self.rearm_ms:
                    self._rested = True
            else:
                self._rest_ms = 0.0
            self._prev_accel = accel

            if magnitude < self.on_threshold_ms2 or not self._rested:
                return None
            self._rested = False
            self._active = True
            self._start_t = t
            self._velocity = np.zeros(3)
            self._peak_velocity = np.zeros(3)
            self._peak_speed = 0.0
            self._peak_accel = magnitude
            self._distance = 0.0
            self._quiet_ms = 0.0
            self._rest_ms = 0.0
            return None

        # Trapezoidal, for the same reason the estimator uses it: a push and
        # its matching brake must cancel to the same accuracy in both.
        previous = self._velocity
        self._velocity = previous + 0.5 * (self._prev_accel + accel) * dt
        self._prev_accel = accel
        self._distance += float(np.linalg.norm(0.5 * (previous + self._velocity))) * dt

        speed = float(np.linalg.norm(self._velocity))
        if speed > self._peak_speed:
            self._peak_speed = speed
            self._peak_velocity = self._velocity.copy()
        self._peak_accel = max(self._peak_accel, magnitude)

        # The acceleration passes through zero halfway through every move, as
        # the push hands over to the brake. Ending on that would cut every
        # event in half, so the event only ends after a sustained quiet spell.
        if magnitude < self.off_threshold_ms2:
            self._quiet_ms += dt * 1000.0
        else:
            self._quiet_ms = 0.0

        duration_ms = (t - self._start_t) * 1000.0
        if self._quiet_ms < self.quiet_ms and duration_ms <= self.max_duration_ms:
            return None

        result = self._finish(t, duration_ms)
        self._active = False
        # Suppression happens here rather than at the trigger, so an event that
        # arrives too soon is captured cleanly and then dropped, instead of
        # being captured badly.
        if (t - self._last_emit_t) * 1000.0 < self.refractory_ms:
            result = None
        self._last_emit_t = t
        return result

    # ------------------------------------------------------------------
    def _finish(self, t: float, duration_ms: float) -> QuickMove | None:
        if duration_ms < self.min_duration_ms or duration_ms > self.max_duration_ms:
            return None
        if self._peak_speed < self.min_speed or self._distance < self.min_distance:
            return None

        direction = self._peak_velocity / self._peak_speed
        in_plane = planarity(direction, self.plane)
        normal = self.normal_axis

        if self.sector_map is None:
            # Axis mode: name the nearest board axis and accept every
            # direction, because there is no sector scheme to be ambiguous
            # about. The caller reads `direction` for the full answer.
            index = int(np.argmax(np.abs(direction)))
            sign = 1 if direction[index] > 0 else -1
            return QuickMove(
                t=t, label=FACE_LABELS[(index, sign)], direction=direction,
                sector=None, vertical=sign if index == normal else 0,
                peak_speed=self._peak_speed, distance=self._distance,
                peak_accel=self._peak_accel, duration_ms=duration_ms,
                planarity=in_plane,
            )

        # A lift or a drop has no meaningful direction within the plane, so it
        # is reported as what it is rather than forced into a sector.
        if abs(direction[normal]) >= self.vertical_fraction:
            sign = 1 if direction[normal] > 0 else -1
            label = self.vertical_labels[0 if sign > 0 else 1]
            return QuickMove(
                t=t, label=label, direction=direction, sector=None, vertical=sign,
                peak_speed=self._peak_speed, distance=self._distance,
                peak_accel=self._peak_accel, duration_ms=duration_ms,
                planarity=in_plane,
            )

        if in_plane < self.min_planarity:
            return None
        sector = self.sector_map.sector_of_vector(direction, self.plane)
        if sector.margin < self.min_margin:
            return None

        return QuickMove(
            t=t, label=sector.label, direction=direction, sector=sector, vertical=0,
            peak_speed=self._peak_speed, distance=self._distance,
            peak_accel=self._peak_accel, duration_ms=duration_ms,
            planarity=in_plane,
        )
