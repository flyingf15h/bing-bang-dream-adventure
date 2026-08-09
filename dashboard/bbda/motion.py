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


def _small_rotation(rotvec: np.ndarray) -> np.ndarray:
    """Rotation matrix for a rotation vector, in radians (Rodrigues).

    Used a step at a time to carry a moving frame along, so the angles are
    always small -- a tenth of a degree at 200 Hz even on a hard flick. The
    exact form is used anyway rather than the ``I + [w]x`` approximation,
    because the approximation is not a rotation: it stretches by
    ``1 + theta^2/2`` every step, and a few hundred steps of that is a frame
    that has quietly grown, which shows up as a direction error that gets
    worse the longer the movement lasts.
    """
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    axis = np.asarray(rotvec, dtype=float) / theta
    cross = np.array([
        [0.0, -axis[2], axis[1]],
        [axis[2], 0.0, -axis[0]],
        [-axis[1], axis[0], 0.0],
    ])
    return (np.eye(3) + math.sin(theta) * cross
            + (1.0 - math.cos(theta)) * (cross @ cross))


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


def levelled_frame(front: np.ndarray, up_world: np.ndarray,
                   fallback: np.ndarray) -> FlickFrame:
    """A flick frame whose "up" is the real up rather than the board's.

    ``up_world`` is which way is actually up, expressed in board axes -- what
    :class:`GravityTracker` produces. It is projected perpendicular to
    ``front`` and normalised, because :class:`FlickFrame` needs an orthonormal
    pair to read a bearing off.

    Why this is the single largest accuracy fix available
    -----------------------------------------------------
    With the board's own ``+Z`` as up, the bearing is measured in the *board's*
    frame, so every degree the board is rolled about its front axis rotates
    every reported direction by that same degree. Hold it dead level and the
    directions are right; hold it canted 25 degrees, as anybody actually does
    while swinging something, and a flick straight up reports 25 degrees off --
    which at a 60 degree lane pitch is most of the way to the neighbouring
    lane. Nothing downstream can undo it, because it is not a fixed offset: it
    changes with how the board happens to be held for that one flick.

    Gravity does not move, so measuring against it makes the answer independent
    of the grip. Roll the board and ``up_world`` rolls with it in board axes;
    the two rotations cancel exactly and the reported bearing does not budge.
    Pitch and yaw of the *front* axis fall out too, because the projection is
    what removes them.

    ``fallback`` is used when the two are too nearly parallel to define a plane
    -- the board pointed at the ceiling or the floor, where "up" genuinely has
    no meaning within the plane the front sweeps and any answer would be the
    direction of rounding error.
    """
    front = np.asarray(front, dtype=float)
    up = np.asarray(up_world, dtype=float)
    up = up - front * float(np.dot(up, front))
    length = float(np.linalg.norm(up))
    # 0.26 is about 15 degrees off vertical. Below that the projection is
    # mostly noise, and a frame built from it would spin freely.
    if length < 0.26:
        return FlickFrame(front=front, up=np.asarray(fallback, dtype=float))
    return FlickFrame(front=front, up=up / length)


# ----------------------------------------------------------------------
# Which way is up, from the board's point of view
# ----------------------------------------------------------------------
class GravityTracker:
    """Follows which way is up, in board axes, through a flick.

    A complementary filter, and a deliberately small one: it tracks a single
    unit vector rather than a full attitude, because a full attitude needs a
    heading reference this does not have and the heading is the one part that
    is not wanted. Two halves:

    * **Propagate with the gyroscope.** A direction fixed in the world, written
      in board axes, turns the opposite way to the board: ``du/dt = -w x u``.
      Over the tenth of a second a flick lasts, an integrated gyroscope is very
      accurate -- this part is what keeps the reference honest *during* the
      movement, when the accelerometer is useless.
    * **Correct with the accelerometer, but only when it can be believed.** A
      still board's accelerometer points straight up and nothing else does; a
      moving one measures gravity plus whatever the hand is doing, and taking
      that as vertical is how a reference ends up chasing the flick it is meant
      to be measured against. So the correction is weighted by how close the
      reading is to 1 g and how slowly the board is turning, and during a real
      flick both terms collapse and the filter simply coasts.

    ``tau`` is how long the accelerometer takes to pull the estimate back when
    it is fully trusted. Long, because the only thing it has to track is how
    somebody is holding the board, which changes over seconds; short values
    just let the swing back in.
    """

    def __init__(
        self,
        tau: float = 0.8,
        accel_tolerance_g: float = 0.18,
        gyro_tolerance_dps: float = 160.0,
    ) -> None:
        self.tau = tau
        self.accel_tolerance_g = accel_tolerance_g
        self.gyro_tolerance_dps = gyro_tolerance_dps
        self.up: np.ndarray | None = None
        #: How much the last accelerometer reading was believed, 0..1. Exposed
        #: so a display can say "the reference is coasting" rather than leaving
        #: a wrong direction unexplained.
        self.trust = 0.0

    def reset(self) -> None:
        self.up = None
        self.trust = 0.0

    @property
    def ready(self) -> bool:
        return self.up is not None

    def update(self, accel_g, gyro_dps, dt: float):
        # Scalars throughout, and numpy only at the boundary.
        #
        # This runs on every sample, which at 400 Hz is 400 times a second on
        # the same thread that has to keep reading the serial port. numpy's
        # cost on a three-element vector is almost entirely per-call overhead
        # -- a few microseconds to dispatch, to do arithmetic that is six
        # multiplies -- so a dozen numpy calls here is a large fraction of the
        # 2.5 ms budget for no arithmetic worth speaking of.
        #
        # Measured: the whole per-sample path cost 0.46 ms of a 2.5 ms budget,
        # a fifth of a core, and the symptom was not slowness. It was that the
        # reader could not keep ahead of the port whenever the game wanted the
        # CPU, so samples queued in the driver and arrived in bursts hundreds
        # of milliseconds late -- which looks exactly like flicks being ignored.
        ax, ay, az = float(accel_g[0]), float(accel_g[1]), float(accel_g[2])
        gx, gy, gz = float(gyro_dps[0]), float(gyro_dps[1]), float(gyro_dps[2])
        magnitude = math.sqrt(ax * ax + ay * ay + az * az)
        rate = math.sqrt(gx * gx + gy * gy + gz * gz)

        if self.up is None:
            # Only start from a reading that looks like gravity and nothing
            # else. Starting from a board mid-swing would seed the filter with
            # a vertical that is off by however hard it was being swung, and
            # tau seconds is a long time to be wrong for.
            if abs(magnitude - 1.0) < self.accel_tolerance_g and \
                    rate < self.gyro_tolerance_dps:
                self.up = (ax / magnitude, ay / magnitude, az / magnitude)
                self.trust = 1.0
            return self.up

        ux, uy, uz = self.up
        if dt > 0.0:
            # du/dt = -w x u, written out. A direction fixed in the world turns
            # the opposite way to the board it is expressed in.
            wx, wy, wz = (math.radians(gx) * dt, math.radians(gy) * dt,
                          math.radians(gz) * dt)
            ux, uy, uz = (ux - (wy * uz - wz * uy),
                          uy - (wz * ux - wx * uz),
                          uz - (wx * uy - wy * ux))
            norm = math.sqrt(ux * ux + uy * uy + uz * uz)
            if norm > 1e-9:
                ux, uy, uz = ux / norm, uy / norm, uz / norm

        # Two Gaussian gates rather than hard cut-offs, so the reference eases
        # back rather than snapping the moment a movement ends -- a step in the
        # vertical between one flick and the next would be a step in every
        # direction reported after it.
        if magnitude > 1e-6:
            level = math.exp(-((magnitude - 1.0) / self.accel_tolerance_g) ** 2)
            still = math.exp(-(rate / self.gyro_tolerance_dps) ** 2)
            self.trust = level * still
            gain = min(1.0, dt / max(1e-6, self.tau)) * self.trust
            if gain > 0.0:
                ux += gain * (ax / magnitude - ux)
                uy += gain * (ay / magnitude - uy)
                uz += gain * (az / magnitude - uz)
                norm = math.sqrt(ux * ux + uy * uy + uz * uz)
                if norm > 1e-9:
                    ux, uy, uz = ux / norm, uy / norm, uz / norm

        self.up = (ux, uy, uz)
        return self.up


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
    t: float             # when the event *ended*; see peak_t
    axis: str            # "x", "y" or "z"
    direction: int       # +1 or -1, right-hand rule about that axis
    peak_dps: float      # peak angular rate on the dominant axis
    peak_accel_g: float  # peak linear acceleration during the event
    dominance: float     # 0..1, how single-axis the rotation was
    duration_ms: float
    sector: Sector | None = None   # set only in sector mode
    peak_vector: np.ndarray = field(default_factory=lambda: np.zeros(3))

    #: The whole stroke as one rotation: the gyroscope integrated from the
    #: first sample of the flick to the last, in degrees, expressed in the
    #: board axes as they were at the *start*. This is where the board ended up
    #: relative to where it began, which is the thing a person means by the
    #: direction of a movement.
    #:
    #: It replaces the single peak sample as the source of the direction, and
    #: the difference is not marginal. One sample is one 5 ms slice of a
    #: gesture: it carries the full gyro noise, it lands wherever the peak
    #: happened to fall, and on a flick whose axis shifts through the stroke --
    #: which is most of them, since a wrist does not rotate about a fixed line
    #: -- it reports the instant rather than the movement. Integrating averages
    #: the noise down by the root of the sample count and answers the question
    #: actually being asked.
    rotation_vector: np.ndarray = field(default_factory=lambda: np.zeros(3))
    #: Total turn over the stroke, degrees. |rotation_vector|.
    rotation_deg: float = 0.0
    #: Where the flick went, degrees clockwise from up, in the frame captured
    #: when it started. Carried on the flick rather than recomputed by callers
    #: because that frame is levelled against gravity at onset and no longer
    #: exists by the time anyone asks.
    bearing_deg: float = float("nan")
    #: The frame the bearing was read in, for anything wanting to show it.
    frame: FlickFrame | None = None
    #: Which way was up, in board axes, when the flick started -- the vertical
    #: ``frame`` was levelled against.
    #:
    #: Carried so that a flick can be re-read against a *different* front axis
    #: afterwards. That is the difference between a tool that can say "these
    #: flicks are inconsistent" and one that can say "they are consistent, and
    #: the front axis is -Y" -- and only the second is any use, because a wrong
    #: front axis is the single most common reason for a board that looks like
    #: it cannot aim. Without this the vertical is gone by the time anyone asks,
    #: and every candidate would have to be judged in the board's own frame,
    #: which is the thing being corrected for.
    up_at_onset: tuple[float, float, float] | None = None
    #: Samples the stroke was integrated over. Small numbers mean the sample
    #: rate is too low for the direction to be trusted.
    samples: int = 0

    #: When the rotation peaked. Anything timing a flick against something
    #: else -- music, most obviously -- wants this and not ``t``.
    #:
    #: A flick cannot be recognised until it is over, so ``t`` is necessarily
    #: the moment the rate fell back below the off threshold. Treating that as
    #: when the flick *happened* puts every one late by roughly half its
    #: duration, and because that is half of a quantity the player varies
    #: freely -- 30 ms for a sharp flick, 75 ms for a lazy one -- it is not a
    #: constant that can be calibrated away. The peak is the middle of the
    #: gesture and the moment the direction is clearest, which makes it the
    #: honest answer to "when did this happen".
    peak_t: float = 0.0

    @property
    def label(self) -> str:
        if self.sector is not None:
            return self.sector.label
        return f"{'+' if self.direction > 0 else '-'}{self.axis.upper()}"


@dataclass
class FlickRejection:
    """A movement that reached the threshold but was not called a flick.

    A refused flick is invisible from the outside: nothing is emitted, so a
    player flicking away at a game that does not respond has no way to tell a
    gesture that was rejected from one the sensor never saw. That is the single
    hardest thing to diagnose about this input, and it is entirely fixable --
    the detector knows exactly which test failed at the moment it fails.

    ``reason`` is a stable key ("swing", "margin", "duration", "dominance"),
    not a sentence, so callers can phrase it for wherever it is being shown.
    """

    t: float
    reason: str
    peak_dps: float
    duration_ms: float
    #: Where the movement went, in degrees clockwise from up. Set whenever a
    #: frame is configured, including for rejections -- knowing which way a
    #: refused gesture went is most of the diagnosis.
    bearing_deg: float = float("nan")
    #: The measured value of whatever test failed, and the floor it missed.
    value: float = 0.0
    limit: float = 0.0


class FlickDetector:
    """Detects a short, sharp rotation and names the axis it happened about.

    A flick is an impulse: angular rate rises past a threshold, peaks, and
    falls back within a few hundred milliseconds. The detector is a small
    state machine rather than a simple threshold so that it can report the
    *peak* of the whole event -- a plain threshold would fire on the leading
    edge, where the axis split is still ambiguous.

    Where the direction comes from
    ------------------------------
    From the stroke as a whole: the gyroscope integrated from the first sample
    to the last, carried in the board axes the flick began in, which is where
    the board *ended up* relative to where it *started*. Not from the single
    fastest sample, which is what this used to use and which has three
    problems that a hand-thrown gesture hits every time. One sample carries the
    full sensor noise where the average of thirty carries a fifth of it. The
    sample that happens to be fastest is chosen by where the peak landed
    between two samples, not by anything about the movement. And a wrist does
    not turn about a fixed axis -- the axis sweeps through the stroke -- so the
    instant of peak rate names where the board was going at that instant, not
    where the movement went.

    Where "up" comes from
    ---------------------
    From gravity, not from the board -- see :func:`levelled_frame`. The board
    is held in a hand, and a hand holds it at whatever angle is comfortable;
    measuring the direction against the board's own axes makes every reported
    direction turn with the grip.

    When it reports
    ---------------
    At the end of the outward stroke, not at the end of the gesture. The rate
    falling back under the off threshold means the hand has finished
    decelerating and has usually begun the return stroke; waiting for it costs
    tens of milliseconds of latency and pulls the return stroke -- a real
    rotation the other way -- into the integral that is measuring the flick.
    See ``commit_fraction``.

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
        commit_fraction: float = 0.6,
        commit_samples: int = 2,
        level_with_gravity: bool = True,
        gravity: GravityTracker | None = None,
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
        #: Fraction of the running peak the rate has to fall to before the
        #: stroke is called finished. See :meth:`update` -- this is the whole
        #: of the latency improvement and the only knob that trades latency
        #: against how much of the stroke is integrated.
        #:
        #: For a flick shaped like a half sine, which is close to what a hand
        #: throws, committing at fraction ``f`` reports at
        #: ``1 - asin(f)/pi`` of the way through and by then has integrated
        #: ``(1 - cos(pi * that))/2`` of the total turn. At the default 0.6
        #: that is 80% of the way through with 90% of the rotation in hand, so
        #: the report comes 0.3 gesture-lengths after the peak instead of the
        #: 0.5 that waiting for the off threshold costs -- around 30 ms rather
        #: than 50 on a typical flick, with a direction that is 90% of the
        #: stroke rather than one sample of it.
        #:
        #: Raising it reports sooner on less of the stroke; 1.0 would report at
        #: the peak itself, which is as early as anything can be known and is
        #: also where the direction is least settled. Lowering it towards the
        #: off threshold restores the old behaviour, return stroke and all.
        self.commit_fraction = commit_fraction
        self.commit_samples = max(1, int(commit_samples))
        self.level_with_gravity = level_with_gravity
        self.gravity = gravity if gravity is not None else GravityTracker()

        self._active = False
        self._start_t = 0.0
        self._prev_t: float | None = None
        self._peak_vector = np.zeros(3)
        self._peak_norm = 0.0
        self._peak_t = 0.0
        self._peak_accel = 0.0
        self._last_emit_t = -1e9
        #: Integrated turn over the stroke so far, degrees, in the board axes
        #: the stroke started in.
        self._rotation = np.zeros(3)
        #: Rotation from the current board axes to the ones at onset, so each
        #: increment can be added in a frame that is not itself moving.
        self._to_onset = np.eye(3)
        self._onset_frame: FlickFrame | None = None
        self._onset_up: tuple | None = None
        self._samples = 0
        self._decaying = 0
        self._reversed = False
        #: The last levelled frame and the vertical it was built from.
        self._frame_cache: tuple | None = None

        #: Why the last completed movement was refused, or None if the last one
        #: was accepted. Read it after :meth:`update` returns None; callers
        #: that do not care can ignore it entirely.
        self.last_rejection: FlickRejection | None = None
        #: Bumped for every rejection, so a caller can tell "a new one" from
        #: "the same one still sitting there" without comparing fields.
        self.rejections = 0

    #: How long a *refused* movement holds the detector off, in ms. Short: long
    #: enough that a movement being refused repeatedly cannot re-trigger on
    #: every sample, and far too short to reach into the flick that follows.
    REFUSED_REARM_MS = 25.0

    def reset(self) -> None:
        self._active = False
        self._prev_t = None
        self._peak_vector = np.zeros(3)
        self._peak_norm = 0.0
        self._peak_t = 0.0
        self._peak_accel = 0.0
        self._last_emit_t = -1e9
        self._rotation = np.zeros(3)
        self._to_onset = np.eye(3)
        self._onset_frame = None
        self._onset_up = None
        self._samples = 0
        self._decaying = 0
        self._reversed = False
        self._frame_cache = None
        self.gravity.reset()

    # ------------------------------------------------------------------
    def live_frame(self) -> FlickFrame | None:
        """The frame to read this flick's bearing in, levelled if it can be.

        Built when the flick starts rather than held as a constant: the whole
        point of levelling is that the answer depends on how the board is being
        held *now*, and a frame computed once at start-up would be exactly the
        fixed board frame it is meant to replace.

        Cached against the vertical it was built from, because callers ask for
        this far more often than it changes. The live arrow wants it on every
        sample -- 400 times a second -- while the thing it depends on is how
        somebody is holding a board, which moves over seconds. Rebuilding it
        each time cost more than the whole rest of the per-sample path.
        """
        if self.frame is None:
            return None
        if not self.level_with_gravity or not self.gravity.ready:
            return self.frame
        up = self.gravity.up
        cached = self._frame_cache
        if cached is not None:
            was, frame = cached
            # A thousandth on each axis is a fifteenth of a degree of tilt, far
            # below anything that changes which lane a flick lands in, and it
            # holds the cache through the small corrections the tracker makes
            # while a board is being held still.
            if (abs(was[0] - up[0]) < 1e-3 and abs(was[1] - up[1]) < 1e-3
                    and abs(was[2] - up[2]) < 1e-3):
                return frame
        frame = levelled_frame(self.frame.front, np.asarray(up, dtype=float),
                               self.frame.up)
        self._frame_cache = (up, frame)
        return frame

    def _refuse(self, t: float, reason: str, duration_ms: float,
                value: float, limit: float,
                turn: np.ndarray | None = None,
                frame: FlickFrame | None = None) -> None:
        """Record why a completed movement was not a flick, and refuse it.

        Returns None so every refusal in :meth:`update` stays a one-line
        ``return self._refuse(...)`` and cannot accidentally fall through into
        emitting the flick it just rejected.

        ``turn`` and ``frame`` are the same integrated rotation and levelled
        frame the flick would have been described by, passed in rather than
        re-derived so a refusal reports the direction the detector actually
        judged. A refusal quoting a different bearing from the one it was
        refused on is worse than quoting none: it sends whoever is reading it
        to tune the wrong thing.
        """
        # Give the refractory back. Nothing was scored, so there is no return
        # stroke owed to anything -- and a refusal is most often a fragment of
        # a movement still in progress, which means the full refractory would
        # be spent blinding the detector to the rest of that very movement. The
        # player would flick, be refused on the first 10 ms of it, and see the
        # remaining 100 ms ignored. A short guard is still kept so a movement
        # that is genuinely being refused over and over cannot spin.
        self._last_emit_t = t - max(
            0.0, self.refractory_ms - self.REFUSED_REARM_MS) / 1000.0

        bearing = float("nan")
        vector = self._peak_vector if turn is None else turn
        reference = frame if frame is not None else self._onset_frame
        if reference is None:
            reference = self.frame
        if reference is not None:
            bearing = float(reference.bearing_deg(vector))
        self.last_rejection = FlickRejection(
            t=t,
            reason=reason,
            peak_dps=float(self._peak_norm),
            duration_ms=float(duration_ms),
            bearing_deg=bearing,
            value=float(value),
            limit=float(limit),
        )
        self.rejections += 1
        return None

    def update(self, t: float, gyro_dps: np.ndarray, accel_g: np.ndarray) -> Flick | None:
        gyro = np.asarray(gyro_dps, dtype=float)
        rate = float(np.linalg.norm(gyro))
        # Linear acceleration magnitude, gravity roughly removed. Body frame is
        # good enough here: this only feeds the reported strength, not the
        # detection itself.
        linear = abs(float(np.linalg.norm(accel_g)) - 1.0)

        # dt off the device clock, taken here rather than asked of the caller so
        # that every existing call site keeps working. Anything absurd -- a
        # first sample, a board that rebooted, a gap in the stream -- is treated
        # as no elapsed time, which costs one sample of integration and is a far
        # better failure than integrating a second of rotation into one step.
        dt = 0.0
        if self._prev_t is not None:
            gap = t - self._prev_t
            if 0.0 < gap < 0.2:
                dt = gap
        self._prev_t = t

        self.gravity.update(accel_g, gyro, dt)

        if not self._active:
            if rate >= self.on_threshold_dps and (t - self._last_emit_t) * 1000.0 >= self.refractory_ms:
                self._active = True
                self._start_t = t
                self._peak_vector = gyro.copy()
                self._peak_norm = rate
                self._peak_t = t
                self._peak_accel = linear
                self._rotation = np.zeros(3)
                self._to_onset = np.eye(3)
                self._onset_frame = self.live_frame()
                self._onset_up = self.gravity.up
                self._samples = 0
                self._decaying = 0
                self._reversed = False
            return None

        # --- event in progress -----------------------------------------
        if rate > self._peak_norm:
            self._peak_norm = rate
            self._peak_vector = gyro.copy()
            self._peak_t = t
        self._peak_accel = max(self._peak_accel, linear)

        if dt > 0.0:
            # Add this step's turn in the axes the flick *started* in. The board
            # is turning while it is being measured, so a step measured in the
            # board's current axes is measured in a frame that has already moved
            # -- add those up directly and a flick that rolls partway through
            # comes out pointing somewhere between where it went and where it
            # would have gone, with the error growing with the roll. Carrying
            # the frame along is the difference between "the board turned this
            # much" and "the board ended up here".
            delta = gyro * dt                       # degrees, current axes
            self._rotation = self._rotation + self._to_onset @ delta
            self._to_onset = self._to_onset @ _small_rotation(np.radians(delta))
            self._samples += 1

        duration_ms = (t - self._start_t) * 1000.0

        # --- has the outward stroke finished? --------------------------
        #
        # Three ways to say yes, and the first two are why this is not the same
        # detector it was. Waiting for the rate to fall under the off threshold
        # means waiting out the whole gesture -- the hand decelerating, and then
        # the return stroke on top -- which is 60 to 100 ms of latency the
        # player feels directly, and it drags the return stroke into the
        # integral, where it cancels the very rotation being measured.
        #
        #   * the rate has fallen to `commit_fraction` of its peak and stayed
        #     there. The peak is behind us and the stroke has spent most of its
        #     turn, so there is nothing left to learn by waiting;
        #   * the rate has reversed against the turn so far, which is the return
        #     stroke starting and is unambiguous;
        #   * the old test, still here as the backstop for a movement that
        #     simply peters out.
        # A reversal only means something once there is a stroke to reverse
        # against. Judged on the accumulated turn rather than on a sample count:
        # at 400 Hz three samples is 7 ms, which on the rising edge of a flick
        # is a direction made mostly of noise, and one unlucky sign flip there
        # ends the event after 10 ms -- too short to be a flick, so it is
        # refused, and the refusal lands in the middle of the movement the
        # player was actually making. Two degrees of turn is well below any
        # real flick and far above anything noise can accumulate.
        reversal = (float(np.linalg.norm(self._rotation)) > 2.0
                    and float(np.dot(gyro, self._rotation)) < 0.0)
        if reversal:
            self._reversed = True
        if rate <= self._peak_norm * self.commit_fraction:
            self._decaying += 1
        else:
            self._decaying = 0

        finished = (
            self._reversed
            or self._decaying >= self.commit_samples
            or rate <= self.off_threshold_dps
            or duration_ms > self.max_duration_ms
        )
        if not finished:
            return None

        self._active = False
        # The refractory starts here and is shortened again by every refusal
        # below. It exists to swallow the return stroke of a flick that
        # *counted*, and charging a refused movement the same 200 ms is how a
        # single stray blip blinds the detector right through the flick that
        # follows it -- which looks, from the outside, exactly like the board
        # ignoring a perfectly good flick. See :meth:`_refuse`.
        self._last_emit_t = t

        if duration_ms < self.min_duration_ms or duration_ms > self.max_duration_ms:
            return self._refuse(t, "duration", duration_ms, duration_ms,
                                self.min_duration_ms if duration_ms < self.min_duration_ms
                                else self.max_duration_ms)
        if self._peak_norm <= 0:
            return None

        # The direction comes off the integrated stroke; the *strength* still
        # comes off the peak, because that is what "how hard" means. On a stroke
        # too short to have been integrated at all, fall back to the peak sample
        # rather than reporting the direction of a zero vector.
        turn = self._rotation
        rotation_deg = float(np.linalg.norm(turn))
        if self._samples < 2 or rotation_deg < 1e-6:
            turn = self._peak_vector
            rotation_deg = float(np.linalg.norm(turn))
        if rotation_deg < 1e-9:
            return None

        index = int(np.argmax(np.abs(turn)))
        axis_dominance = abs(turn[index]) / rotation_deg
        frame = self._onset_frame
        bearing = float("nan")

        sector = None
        if self.sector_map is not None and frame is not None:
            # Bearing mode: a roll about the front is refused here, since it
            # leaves the front pointing where it was and so has no direction.
            swing = frame.swing_fraction(turn)
            if swing < self.min_dominance:
                return self._refuse(t, "swing", duration_ms, swing,
                                    self.min_dominance, turn=turn, frame=frame)
            bearing = float(frame.bearing_deg(turn))
            sector = self.sector_map.sector_of(bearing)
            if sector.margin < self.min_margin:
                return self._refuse(t, "margin", duration_ms, sector.margin,
                                    self.min_margin, turn=turn, frame=frame)
            dominance = swing
        elif self.sector_map is not None:
            # In sector mode "dominance" means how much of the rotation lay in
            # the plane being divided up, not how much lay on one axis.
            in_plane = planarity(turn, self.plane)
            if in_plane < self.min_dominance:
                return self._refuse(t, "planarity", duration_ms, in_plane,
                                    self.min_dominance, turn=turn, frame=frame)
            sector = self.sector_map.sector_of_vector(turn, self.plane)
            if sector.margin < self.min_margin:
                return self._refuse(t, "margin", duration_ms, sector.margin,
                                    self.min_margin, turn=turn, frame=frame)
            dominance = in_plane
        else:
            if axis_dominance < self.min_dominance:
                return self._refuse(t, "dominance", duration_ms, axis_dominance,
                                    self.min_dominance, turn=turn, frame=frame)
            dominance = axis_dominance
            if frame is not None:
                bearing = float(frame.bearing_deg(turn))

        self.last_rejection = None
        return Flick(
            t=t,
            axis=AXIS_NAMES[index],
            direction=1 if turn[index] > 0 else -1,
            peak_dps=abs(float(self._peak_vector[index])),
            peak_accel_g=self._peak_accel,
            dominance=dominance,
            duration_ms=duration_ms,
            sector=sector,
            peak_vector=self._peak_vector.copy(),
            peak_t=self._peak_t,
            rotation_vector=np.asarray(turn, dtype=float).copy(),
            rotation_deg=rotation_deg,
            bearing_deg=bearing,
            frame=frame,
            up_at_onset=self._onset_up,
            samples=self._samples,
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
