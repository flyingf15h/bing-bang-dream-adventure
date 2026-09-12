"""Calibration model and estimators.

Three corrections are supported, matching what the firmware stores in NVS:

* gyroscope bias -- a constant offset per axis, in dps.
* accelerometer bias and per-axis gain -- from a six-position measurement.
* magnetometer hard- and soft-iron -- from an ellipsoid fit over a rotation.

The correction applied everywhere is::

    gyro_out  = gyro_raw - gyro_bias
    accel_out = (accel_raw - accel_bias) * accel_scale
    mag_out   = mag_soft @ (mag_raw - mag_bias)

which is exactly the model in bbda_imu.ino, so a calibration computed here
can be pushed to the board and behave identically there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# The six orientations of the accelerometer routine, in capture order.
SIX_POSITIONS = (
    ("+Z up", "z", +1, "Board flat, component side up"),
    ("-Z up", "z", -1, "Board flat, component side down"),
    ("+X up", "x", +1, "Board on its edge, +X axis pointing at the ceiling"),
    ("-X up", "x", -1, "Board on its edge, -X axis pointing at the ceiling"),
    ("+Y up", "y", +1, "Board on its edge, +Y axis pointing at the ceiling"),
    ("-Y up", "y", -1, "Board on its edge, -Y axis pointing at the ceiling"),
)

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


# ----------------------------------------------------------------------
# Axis alignment (mounting orientation)
# ----------------------------------------------------------------------
def right_handed_alignments() -> list[tuple[str, np.ndarray]]:
    """Every signed axis permutation that is a real rotation.

    Of the 48 ways to map the three board axes onto the three display axes
    with signs, only 24 have determinant +1. The other 24 are mirrors: they
    would flip the handedness of the frame, which silently reverses the sense
    of every gyroscope reading and makes the model rotate backwards. Offering
    only the valid 24 makes that mistake unreachable from the UI.
    """
    import itertools

    labels = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")
    vectors = {
        "+X": np.array([1.0, 0, 0]), "-X": np.array([-1.0, 0, 0]),
        "+Y": np.array([0, 1.0, 0]), "-Y": np.array([0, -1.0, 0]),
        "+Z": np.array([0, 0, 1.0]), "-Z": np.array([0, 0, -1.0]),
    }

    out: list[tuple[str, np.ndarray]] = []
    for combo in itertools.permutations(labels, 3):
        axes = {label[1] for label in combo}
        if len(axes) != 3:
            continue  # same physical axis used twice
        matrix = np.array([vectors[label] for label in combo])
        if abs(np.linalg.det(matrix) - 1.0) > 1e-9:
            continue
        name = f"X→{combo[0]}  Y→{combo[1]}  Z→{combo[2]}"
        if np.allclose(matrix, np.eye(3)):
            name += "   (identity)"
        out.append((name, matrix))

    out.sort(key=lambda item: "identity" not in item[0])
    return out


def alignment_name(matrix: np.ndarray) -> str:
    """Name of the alignment closest to ``matrix``, for display."""
    for name, candidate in right_handed_alignments():
        if np.allclose(candidate, matrix, atol=1e-6):
            return name
    return "custom"


# ----------------------------------------------------------------------
# Working out the alignment by moving the board
# ----------------------------------------------------------------------
# The frame the dashboard draws in: X forward (away from you), Y left, Z up.
# It is right-handed, which matters -- see right_handed_alignments().
MOVE_DIRECTIONS = (
    ("forward", np.array([1.0, 0.0, 0.0]), "Slide it straight away from you"),
    ("back", np.array([-1.0, 0.0, 0.0]), "Slide it straight towards you"),
    ("left", np.array([0.0, 1.0, 0.0]), "Slide it straight to your left"),
    ("right", np.array([0.0, -1.0, 0.0]), "Slide it straight to your right"),
    ("up", np.array([0.0, 0.0, 1.0]), "Lift it straight up"),
    ("down", np.array([0.0, 0.0, -1.0]), "Lower it straight down"),
)

MOVE_TARGETS = {name: vector for name, vector, _hint in MOVE_DIRECTIONS}

# Easy mode asks only for horizontal slides, because gravity gives it the
# vertical axis for free -- see solve_frame_from_gravity_and_moves(). The value
# is the angle, measured about "up", that carries that slide's direction onto
# forward, which is how each slide becomes an estimate of the same thing.
_SLIDE_TURN_DEG = {
    "forward": 0.0,
    "right": 90.0,
    "back": 180.0,
    "left": -90.0,
}


@dataclass
class FrameFitResult:
    """Outcome of solving the mounting orientation from a set of moves."""

    matrix: np.ndarray                  # best-fit rotation, board -> display
    snapped: np.ndarray                 # nearest of the 24 axis alignments
    snapped_name: str
    snap_error_deg: float               # angle between the two above
    residuals_deg: dict[str, float]     # per-move angle error after fitting
    worst_deg: float
    ok: bool
    message: str


def snap_to_axis_alignment(matrix: np.ndarray) -> tuple[str, np.ndarray, float]:
    """Nearest right-handed axis permutation, and how far away it was.

    Returns the name, the matrix, and the rotation angle in degrees between
    the input and that permutation. The angle is the useful part: a board
    mounted squarely gives a few degrees, and anything above about 20 says the
    board is not axis-aligned with the way it was moved -- either it is
    genuinely mounted at an angle, or the moves were sloppy.
    """
    best_name, best_matrix, best_trace = "", np.eye(3), -np.inf
    for name, candidate in right_handed_alignments():
        # For two rotations, the larger tr(A^T B) is, the smaller the angle
        # between them -- tr = 1 + 2 cos(angle).
        score = float(np.trace(matrix.T @ candidate))
        if score > best_trace:
            best_name, best_matrix, best_trace = name, candidate, score
    cosine = max(-1.0, min(1.0, (best_trace - 1.0) / 2.0))
    return best_name, best_matrix, float(np.degrees(np.arccos(cosine)))


def solve_frame_from_moves(
    measurements: dict[str, np.ndarray],
    max_residual_deg: float = 25.0,
) -> FrameFitResult:
    """Find the board-to-display rotation from directed movements.

    The general estimator: any set of directed moves, including vertical ones.
    Easy mode no longer calls it -- see
    :func:`solve_frame_from_gravity_and_moves` for why measuring the vertical
    axis by lifting the board is a worse way to learn something gravity gives
    away for free -- but it remains the right tool where the moves cannot be
    assumed horizontal, and it is what a from-scratch frame fit should use.

    Each entry maps a move name from :data:`MOVE_DIRECTIONS` to the direction
    that move was measured to have, expressed in *board* axes. The job is to
    find the single rotation that best carries each measured direction onto
    the direction it was supposed to be.

    That is Wahba's problem, and its closed-form solution is Kabsch's
    algorithm: form ``H = sum(measured_i * target_i^T)``, take its singular
    value decomposition, and read the rotation off directly. Two properties
    make it the right tool rather than an over-engineered one:

    * it uses every measurement at once, so five sloppy moves beat two careful
      ones instead of the last one winning;
    * the ``diag(1, 1, d)`` correction guarantees a genuine rotation comes out
      rather than a reflection, which is the exact failure that would silently
      reverse the sense of every gyroscope reading.

    Two non-parallel moves are enough to fix a frame; the rest is redundancy,
    and the per-move residuals it makes available are what turn "here is an
    answer" into "here is an answer, and here is how much to trust it".
    """
    usable: list[tuple[str, np.ndarray, np.ndarray]] = []
    for name, measured in measurements.items():
        target = MOVE_TARGETS.get(name)
        if target is None:
            continue
        vector = np.asarray(measured, dtype=float)
        norm = float(np.linalg.norm(vector))
        if norm < 1e-9 or not np.all(np.isfinite(vector)):
            continue
        usable.append((name, vector / norm, target))

    if len(usable) < 2:
        return _frame_failure("At least two different movements are needed.")

    covariance = np.zeros((3, 3))
    for _name, measured, target in usable:
        covariance += np.outer(measured, target)

    u, singular, vt = np.linalg.svd(covariance)
    # Two parallel moves span only one direction, so the second singular value
    # collapses and the rotation about that axis is unconstrained. Saying so
    # beats returning whichever arbitrary answer the SVD happened to pick.
    if singular[1] < 1e-6:
        return _frame_failure(
            "Those movements were all along the same line; add one at right "
            "angles to them."
        )

    handedness = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    matrix = vt.T @ np.diag([1.0, 1.0, handedness]) @ u.T

    residuals: dict[str, float] = {}
    for name, measured, target in usable:
        cosine = max(-1.0, min(1.0, float(np.dot(matrix @ measured, target))))
        residuals[name] = float(np.degrees(np.arccos(cosine)))
    worst = max(residuals.values())

    snapped_name, snapped, snap_error = snap_to_axis_alignment(matrix)

    ok = worst <= max_residual_deg
    if ok:
        message = (
            f"Solved from {len(usable)} movements; worst movement was "
            f"{worst:.0f}° off, and the result is {snap_error:.0f}° from "
            f"{snapped_name}."
        )
    else:
        worst_name = max(residuals, key=residuals.get)
        message = (
            f"The '{worst_name}' movement disagrees with the others by "
            f"{worst:.0f}°. Redo it, keeping the board flat and moving in a "
            "straight line without turning it."
        )

    return FrameFitResult(
        matrix=matrix,
        snapped=snapped,
        snapped_name=snapped_name,
        snap_error_deg=snap_error,
        residuals_deg=residuals,
        worst_deg=worst,
        ok=ok,
        message=message,
    )


def solve_frame_from_gravity_and_moves(
    gravity_board: np.ndarray,
    moves: dict[str, np.ndarray],
    max_disagreement_deg: float = 40.0,
    min_horizontal: float = 0.35,
) -> FrameFitResult:
    """Find the board-to-display rotation from gravity plus horizontal slides.

    This is what easy mode uses, and it is deliberately cruder than
    :func:`solve_frame_from_moves`. The job there is only to work out *which
    board axis is which* -- one of 24 discrete answers -- so accuracy beyond
    "nearer this axis than that one" buys nothing, while asking a person to
    move a board precisely costs a great deal. Two things follow.

    **Up comes from gravity, not from a lift.** A board resting on a table
    reads +1 g along whichever of its axes points at the sky, to a fraction of
    a degree, with no skill required at all. Asking someone to lift it
    straight up measures the same thing far worse: a hand tilts, and five
    degrees of tilt leaks 0.09 g of phantom horizontal acceleration into the
    reading -- the same order as the whole of a gentle lift. One free and
    exact measurement replaces two awkward and poor ones, and that alone
    removes two of the five movements the old routine asked for.

    **The slides only have to pick a horizontal axis.** Each slide is
    projected onto the plane perpendicular to gravity, which discards exactly
    the component that tilting corrupts, and then rotated about gravity into
    an estimate of "forward". Because the result is snapped to one of the 24
    afterwards, any slide within 45 degrees of the direction asked for lands
    on the right answer -- so one roughly-aimed shove is enough and the second
    slide is confirmation, not a requirement.

    ``moves`` maps a name from :data:`_SLIDE_TURN_DEG` to the direction that
    slide was measured to have, in *board* axes. ``gravity_board`` is what the
    accelerometer reads while the board sits still, also in board axes.
    """
    gravity = np.asarray(gravity_board, dtype=float)
    norm = float(np.linalg.norm(gravity))
    if norm < 1e-9 or not np.all(np.isfinite(gravity)):
        return _frame_failure(
            "The board never sat still long enough to work out which way is up."
        )
    up = gravity / norm

    estimates: dict[str, np.ndarray] = {}
    lifted: list[str] = []
    for name, measured in moves.items():
        turn = _SLIDE_TURN_DEG.get(name)
        if turn is None:
            continue
        vector = np.asarray(measured, dtype=float)
        if vector.shape != (3,) or not np.all(np.isfinite(vector)):
            continue
        length = float(np.linalg.norm(vector))
        if length < 1e-9:
            continue
        flat = vector - up * float(np.dot(vector, up))
        flat_norm = float(np.linalg.norm(flat))
        if flat_norm < min_horizontal * length:
            # Went almost straight up or down. Nothing about a vertical move
            # says which way along the table is forward, however neat it was.
            lifted.append(name)
            continue
        flat /= flat_norm
        angle = np.radians(turn)
        # Rodrigues' formula, with the term that vanishes because `flat` is
        # already perpendicular to `up` left out.
        estimates[name] = flat * np.cos(angle) + np.cross(up, flat) * np.sin(angle)

    if not estimates:
        return _frame_failure(
            "That did not travel along the table. Slide the board flat across "
            "the surface rather than lifting or tilting it."
        )

    total = np.sum(list(estimates.values()), axis=0)
    if float(np.linalg.norm(total)) < 1e-6:
        # Slides that cancelled each other out: keep the first one rather than
        # return a direction assembled out of rounding error.
        forward = next(iter(estimates.values()))
    else:
        forward = total / float(np.linalg.norm(total))
    forward = forward - up * float(np.dot(forward, up))
    forward /= float(np.linalg.norm(forward))

    # Rows, because ``mount @ board`` must give display coordinates: the first
    # row dotted with a board vector is its forward component, and so on.
    left = np.cross(up, forward)
    matrix = np.array([forward, left, up])

    residuals = {
        name: float(np.degrees(np.arccos(
            max(-1.0, min(1.0, float(np.dot(vector, forward))))
        )))
        for name, vector in estimates.items()
    }
    worst = max(residuals.values())

    snapped_name, snapped, snap_error = snap_to_axis_alignment(matrix)

    ok = worst <= max_disagreement_deg
    if ok:
        slides = f"{len(estimates)} slide" + ("" if len(estimates) == 1 else "s")
        message = f"Solved from gravity and {slides}; the result is {snapped_name}."
        if lifted:
            message += (
                " The " + ", ".join(lifted) + " slide went up rather than along "
                "the table, so it was left out."
            )
    else:
        worst_name = max(residuals, key=residuals.get)
        message = (
            f"The '{worst_name}' slide points {worst:.0f}° away from what the "
            "others say. Redo the step, sliding the board flat across the table "
            "without turning it."
        )

    return FrameFitResult(
        matrix=matrix,
        snapped=snapped,
        snapped_name=snapped_name,
        snap_error_deg=snap_error,
        residuals_deg=residuals,
        worst_deg=worst,
        ok=ok,
        message=message,
    )


def _frame_failure(message: str) -> FrameFitResult:
    return FrameFitResult(
        matrix=np.eye(3),
        snapped=np.eye(3),
        snapped_name=alignment_name(np.eye(3)),
        snap_error_deg=0.0,
        residuals_deg={},
        worst_deg=180.0,
        ok=False,
        message=message,
    )


@dataclass
class Calibration:
    """A complete correction set."""

    gyro_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    accel_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    accel_scale: np.ndarray = field(default_factory=lambda: np.ones(3))
    mag_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    mag_soft: np.ndarray = field(default_factory=lambda: np.eye(3))

    # Mounting orientation: maps board axes onto the axes the dashboard draws.
    # Host-side only -- the firmware has no use for it, since it never fuses
    # or renders. Applied after the per-sensor corrections and before fusion,
    # so all three sensors stay in one consistent frame.
    mount: np.ndarray = field(default_factory=lambda: np.eye(3))

    # ------------------------------------------------------------------
    # The correct_* methods stop at the sensor correction and stay in *board*
    # axes. Anything trying to work out the mounting orientation has to use
    # them, because applying ``mount`` first would fold the answer it is
    # looking for into its own input.
    def correct_gyro(self, gyro: np.ndarray) -> np.ndarray:
        return np.asarray(gyro, dtype=float) - self.gyro_bias

    def correct_accel(self, accel: np.ndarray) -> np.ndarray:
        return (np.asarray(accel, dtype=float) - self.accel_bias) * self.accel_scale

    def correct_mag(self, mag: np.ndarray) -> np.ndarray:
        return self.mag_soft @ (np.asarray(mag, dtype=float) - self.mag_bias)

    def apply_gyro(self, gyro: np.ndarray) -> np.ndarray:
        return self.mount @ self.correct_gyro(gyro)

    def apply_accel(self, accel: np.ndarray) -> np.ndarray:
        return self.mount @ self.correct_accel(accel)

    def apply_mag(self, mag: np.ndarray) -> np.ndarray:
        return self.mount @ self.correct_mag(mag)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "gyro_bias": self.gyro_bias.tolist(),
            "accel_bias": self.accel_bias.tolist(),
            "accel_scale": self.accel_scale.tolist(),
            "mag_bias": self.mag_bias.tolist(),
            "mag_soft": self.mag_soft.tolist(),
            "mount": self.mount.tolist(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        return cls(
            gyro_bias=np.array(data.get("gyro_bias", [0, 0, 0]), dtype=float),
            accel_bias=np.array(data.get("accel_bias", [0, 0, 0]), dtype=float),
            accel_scale=np.array(data.get("accel_scale", [1, 1, 1]), dtype=float),
            mag_bias=np.array(data.get("mag_bias", [0, 0, 0]), dtype=float),
            mag_soft=np.array(data.get("mag_soft", np.eye(3).tolist()), dtype=float),
            mount=np.array(data.get("mount", np.eye(3).tolist()), dtype=float),
        )

    def save(self, path: str | Path) -> Path:
        """Write the calibration as JSON, making the folder if it is missing.

        The folder is created because the autosave path lives in one of its
        own; a save that failed only because nothing had made a directory yet
        would be a poor way to lose a calibration.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    def to_device_commands(self) -> list[str]:
        """Firmware command lines that reproduce this calibration on-board.

        ``mount`` is deliberately absent: it is a display convention, and
        pushing it would rotate the board's own printed readings away from the
        physical axes silkscreened on the PCB.
        """

        def triple(v: np.ndarray) -> str:
            return " ".join(f"{x:.6f}" for x in np.asarray(v).ravel())

        return [
            f"cal gyro {triple(self.gyro_bias)}",
            f"cal abias {triple(self.accel_bias)}",
            f"cal ascale {triple(self.accel_scale)}",
            f"cal mbias {triple(self.mag_bias)}",
            f"cal msoft {triple(self.mag_soft)}",
            "cal save",
        ]


# ----------------------------------------------------------------------
# The working copy on disk
# ----------------------------------------------------------------------
#: Where the dashboard keeps the calibration it is currently using. It is
#: rewritten the moment any step produces a result and read back at startup,
#: so nothing measured is ever only in memory: closing the window, a crash, or
#: the cable coming out mid-way through easy mode all cost nothing but the
#: step in progress. The explicit Save/Load buttons are for named copies
#: somewhere else; this one needs no decision from anybody.
AUTOSAVE_PATH = Path.home() / ".bbda" / "calibration.json"


def load_autosave(path: str | Path = AUTOSAVE_PATH) -> "Calibration | None":
    """The working copy, or ``None`` if there is no readable one.

    Quiet about failure on purpose: no file is the ordinary first-run case,
    and a corrupt one should start the program with a fresh calibration rather
    than not start it at all.
    """
    try:
        return Calibration.load(path)
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------
# Gyroscope
# ----------------------------------------------------------------------
class GyroBiasCollector:
    """Averages the gyroscope while the board is held still.

    Also reports the peak-to-peak spread, which is how the UI can tell the
    user they moved the board mid-capture.
    """

    def __init__(self, target: int = 500) -> None:
        self.target = target
        self._samples: list[np.ndarray] = []

    def add(self, gyro: np.ndarray) -> None:
        if len(self._samples) < self.target:
            self._samples.append(np.asarray(gyro, dtype=float).copy())

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def done(self) -> bool:
        return len(self._samples) >= self.target

    def result(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (bias, peak-to-peak spread), both in dps."""
        if not self._samples:
            return np.zeros(3), np.zeros(3)
        data = np.array(self._samples)
        return data.mean(axis=0), data.max(axis=0) - data.min(axis=0)


# ----------------------------------------------------------------------
# Accelerometer
# ----------------------------------------------------------------------
class AccelSixPointCollector:
    """Six-position accelerometer bias and gain estimate.

    For each axis, the readings with that axis up and down bracket the
    +/-1 g span. The midpoint of the span is the offset and half the span is
    the gain error, which is the standard six-position method.
    """

    def __init__(self, samples_per_position: int = 200) -> None:
        self.samples_per_position = samples_per_position
        self.captured: dict[str, np.ndarray] = {}
        self._active: str | None = None
        self._buffer: list[np.ndarray] = []
        #: Sentences about axes whose gain was refused by :meth:`result`, ready
        #: to show. Empty when every axis was measured properly.
        self.rejected: list[str] = []

    def start(self, position_name: str) -> None:
        self._active = position_name
        self._buffer = []

    def add(self, accel: np.ndarray) -> None:
        if self._active is None:
            return
        if len(self._buffer) < self.samples_per_position:
            self._buffer.append(np.asarray(accel, dtype=float).copy())
        if len(self._buffer) >= self.samples_per_position:
            self.captured[self._active] = np.array(self._buffer).mean(axis=0)
            self._active = None

    @property
    def capturing(self) -> bool:
        return self._active is not None

    @property
    def progress(self) -> float:
        if self._active is None:
            return 0.0
        return len(self._buffer) / self.samples_per_position

    @property
    def complete(self) -> bool:
        return all(name in self.captured for name, *_ in SIX_POSITIONS)

    #: How far a per-axis gain may sit from 1.0 and still be believed.
    #:
    #: An accelerometer's sensitivity error is a trim, not a discovery: this
    #: part is specified to a couple of percent and a bad one is out by ten.
    #: Anything past this did not measure a real gain -- it measured two
    #: captures that were not actually a half turn apart, which is what
    #: happens when a face gets recorded twice, and it is silent because the
    #: arithmetic is perfectly happy with it.
    #:
    #: A board found in the wild carried 26.3 and 74.8 on two axes from
    #: exactly that. Nothing complained; the accelerometer simply read 3.7 g
    #: lying still, every reading that depended on gravity was wrong, and the
    #: only visible symptom was that flicks went in odd directions.
    MAX_GAIN_ERROR = 0.25

    def result(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (bias in g, per-axis scale factor).

        An axis whose two captures do not describe a proper half turn keeps a
        gain of exactly 1.0 and is listed in :attr:`rejected`, because leaving
        the axis uncorrected is wrong by a couple of percent while trusting the
        measurement is wrong by a factor of twenty.
        """
        bias = np.zeros(3)
        scale = np.ones(3)
        self.rejected = []
        for axis, index in _AXIS_INDEX.items():
            up = down = None
            for name, ax, sign, _ in SIX_POSITIONS:
                if ax != axis or name not in self.captured:
                    continue
                if sign > 0:
                    up = self.captured[name][index]
                else:
                    down = self.captured[name][index]
            if up is None or down is None:
                continue
            bias[index] = (up + down) / 2.0
            # Turning the board over swaps which way gravity pulls along this
            # axis, so the two readings must be about 2 g apart. Anything else
            # means they were not taken from opposite faces.
            half_span = (up - down) / 2.0
            gain = 1.0 / half_span if abs(half_span) > 1e-6 else 0.0
            if abs(half_span - 1.0) > self.MAX_GAIN_ERROR:
                self.rejected.append(
                    f"{axis.upper()}: the two captures are {abs(up - down):.2f} g "
                    f"apart and turning the board over should make them 2 g "
                    f"apart, so one of the {axis.upper()} positions was not "
                    f"held the way it was asked for. Redo them; the gain that "
                    f"would have been stored is {gain:.1f}, and 1.0 is right."
                )
                continue
            scale[index] = gain
        return bias, scale


# ----------------------------------------------------------------------
# Magnetometer
# ----------------------------------------------------------------------
@dataclass
class MagFitResult:
    bias: np.ndarray            # hard-iron offset, uT
    soft: np.ndarray            # 3x3 soft-iron matrix
    field_strength: float       # fitted sphere radius, uT
    residual_pct: float         # spread of the corrected radius, % of radius
    radii: np.ndarray           # ellipsoid semi-axes before correction, uT
    ok: bool
    message: str


def fit_ellipsoid(points: np.ndarray) -> MagFitResult:
    """Fit an ellipsoid to magnetometer samples and derive the correction.

    Solves the general quadric ``x'Mx + 2n'x = 1`` in the least-squares
    sense, completes the square to recover the centre (the hard-iron
    offset), then takes the matrix square root of the normalised quadratic
    form to get the transform that maps the ellipsoid onto a sphere (the
    soft-iron matrix). The sphere is scaled back to the fitted field
    strength so the corrected output stays in microtesla.
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] < 30:
        return _fit_failure("Need at least 30 samples spread over a full rotation.")

    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    design = np.column_stack(
        [x * x, y * y, z * z, 2 * x * y, 2 * x * z, 2 * y * z, 2 * x, 2 * y, 2 * z]
    )
    target = np.ones(pts.shape[0])

    try:
        params, *_ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError:
        return _fit_failure("The fit did not converge; collect more coverage.")

    a, b, c, d, e, f, g, h, i = params
    quad = np.array([[a, d, e], [d, b, f], [e, f, c]])
    lin = np.array([g, h, i])

    try:
        centre = -np.linalg.solve(quad, lin)
    except np.linalg.LinAlgError:
        return _fit_failure("Samples are close to coplanar; rotate through more axes.")

    k = 1.0 - lin @ centre
    if k <= 0:
        return _fit_failure("Fit produced a degenerate surface; collect again.")

    form = quad / k
    eigenvalues, eigenvectors = np.linalg.eigh(form)
    if np.any(eigenvalues <= 0):
        return _fit_failure("Fit is not an ellipsoid; rotate through more axes.")

    radii = 1.0 / np.sqrt(eigenvalues)
    field = float(np.prod(radii) ** (1.0 / 3.0))

    # Matrix square root of the quadratic form maps the ellipsoid to a unit
    # sphere; scaling by the mean radius keeps the output in microtesla.
    sphere_map = eigenvectors @ np.diag(np.sqrt(eigenvalues)) @ eigenvectors.T
    soft = field * sphere_map

    corrected = (soft @ (pts - centre).T).T
    norms = np.linalg.norm(corrected, axis=1)
    residual = float(norms.std() / norms.mean() * 100.0) if norms.mean() > 0 else 0.0

    return MagFitResult(
        bias=centre,
        soft=soft,
        field_strength=field,
        residual_pct=residual,
        radii=radii,
        ok=True,
        message=(
            f"Fitted field {field:.1f} uT, residual {residual:.2f}% "
            f"over {pts.shape[0]} samples."
        ),
    )


def _fit_failure(message: str) -> MagFitResult:
    return MagFitResult(
        bias=np.zeros(3),
        soft=np.eye(3),
        field_strength=0.0,
        residual_pct=0.0,
        radii=np.zeros(3),
        ok=False,
        message=message,
    )


class MagCollector:
    """Accumulates magnetometer samples, thinned by distance.

    Rotating a board by hand produces long runs of near-identical samples.
    Keeping only points that are at least ``min_spacing`` microtesla from
    every stored point keeps the fit balanced instead of letting whichever
    orientation was held longest dominate it.
    """

    def __init__(self, min_spacing: float = 1.5, limit: int = 3000) -> None:
        self.min_spacing = min_spacing
        self.limit = limit
        # Preallocated, because this runs on every incoming sample: rebuilding
        # an array from a list here would make collection quadratic and stall
        # the GUI well before the point limit is reached.
        self._buffer = np.zeros((limit, 3), dtype=float)
        self._count = 0

    def add(self, mag: np.ndarray) -> bool:
        """Store the sample if it is far enough from the existing set."""
        point = np.asarray(mag, dtype=float)
        if point.shape != (3,) or not np.all(np.isfinite(point)):
            return False
        if np.linalg.norm(point) < 1e-6 or self._count >= self.limit:
            return False
        if self._count:
            delta = self._buffer[: self._count] - point
            # Compare squared distances to skip a whole array of square roots.
            if np.einsum("ij,ij->i", delta, delta).min() < self.min_spacing ** 2:
                return False
        self._buffer[self._count] = point
        self._count += 1
        return True

    def clear(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def points(self) -> np.ndarray:
        return self._buffer[: self._count].copy()

    def coverage(self) -> float:
        """Rough fraction of the sphere visited, from octant occupancy.

        Not a rigorous solid-angle measure, but it is enough to tell someone
        they have only turned the board around one axis.
        """
        if self._count < 8:
            return 0.0
        pts = self._buffer[: self._count]
        centred = pts - pts.mean(axis=0)
        # One bit per axis sign gives an octant index in 0..7.
        codes = ((centred >= 0) * np.array([1, 2, 4])).sum(axis=1)
        return len(np.unique(codes)) / 8.0

    def fit(self) -> MagFitResult:
        return fit_ellipsoid(self.points)
