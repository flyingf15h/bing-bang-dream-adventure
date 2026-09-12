"""Headless calibration: the six-position/mag/orientation sequence, without a
screen of its own.

This is `bbda/wizard.py`'s "easy mode" (see git history on `main` -- it is not
deleted, only unshipped) restated as a state machine that a game can drive
over the wire instead of a PySide6 widget driving on-screen labels. The
maths it calls -- :mod:`bbda.calibration`, :mod:`bbda.motion` -- is frozen and
untouched; only the sequencing and the thresholds for "is this step good
enough yet" are ported here, verbatim, from the wizard. Guessing new ones
means a wizard that accepts a bad calibration, which fails silently, days
later, as "the controller feels wrong".

Steps, in order for a ``"full"`` sequence: rest (gyro bias) -> six sides
(accelerometer) -> wave it around (magnetometer) -> which way is which
(mounting orientation) -> check (verify against physics that must hold).
A ``"quick"`` sequence skips straight to orientation, reusing whatever
gyro/accel/mag correction the last full calibration already found -- for
when a board moves to a new desk and only its mounting needs redoing, not a
five-minute recapture of physics that has not changed.

Each step auto-advances the moment its own criterion is met; nobody has to
press anything to move from "hold still" to "six sides" once the bias is
good. The one place a step genuinely cannot decide for itself is the second,
confirming slide in the orientation step -- one slide is already a complete
answer, a second only tightens it, and whether that trade is worth making is
a judgement call for whoever is holding the board. That is what
:meth:`CalSequence.advance` is for. Saving is its own command
(``cal_save``, handled where the link is), not `advance()`, because writing
to the board is consequential enough to want its own explicit trigger rather
than falling out of "the player pressed Continue one more time".
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import protocol
from .calibration import (
    SIX_POSITIONS,
    AccelSixPointCollector,
    Calibration,
    GyroBiasCollector,
    MagCollector,
    alignment_name,
    solve_frame_from_gravity_and_moves,
)
from .link import Sample
from .motion import G_MS2, FlipDetector, QuickMoveDetector, StationaryDetector

# ----------------------------------------------------------------------
# Thresholds and prompts, ported verbatim from bbda/wizard.py
# ----------------------------------------------------------------------

#: Samples averaged into one gravity reading during the orientation step, and
#: how far apart in g the accelerometer readings within that run may be for it
#: to count as one settled run. The spread test is what tells a level, still
#: board apart from one that is mid-slide: a horizontal slide barely moves
#: |accel|, so StationaryDetector alone cannot see it, and averaging through
#: one would quietly fold part of a push into the gravity reference the push
#: is measured against.
GRAVITY_SAMPLES = 60
GRAVITY_SPREAD_G = 0.015

#: Samples of re-settling between slides before the game is told it is taking
#: a while (nothing is wrong; this only stops long silences reading as a hang).
SETTLE_NAG_SAMPLES = 400

#: Degrees the board may turn about vertical during a slide before the slide
#: is rejected. An angle rather than a rate: what corrupts the answer is net
#: rotation over the stroke, not how fast it happened, and a brief wobble that
#: nets zero is most of what a hand does to a board being shoved across a desk.
MOVE_TURN_LIMIT_DEG = 35.0

#: The two slides the orientation step asks for, in order. The second is
#: optional -- gravity already supplies the vertical axis, so one horizontal
#: slide completes the answer and the other only confirms it.
MOVE_SEQUENCE = (
    ("forward", "Shove the board away from you, then let it stop."),
    ("right", "Now shove the board to your right, then let it stop. "
              "This only double-checks the first slide -- send cal_advance "
              "instead if you would rather skip it."),
)

#: Thresholds for the slide detector, all well below FlickDetector-style
#: defaults. The player has been asked for one specific movement and is
#: waiting for it to register, so missing a real slide is much worse than
#: accepting a feeble one; the speed and distance floors below discard a false
#: trigger before it can name an axis.
SLIDE_DETECTOR = dict(
    sector_map=None,        # discovering the frame, so accept any direction
    on_threshold_ms2=0.35,
    off_threshold_ms2=0.15,
    quiet_ms=250.0,
    rearm_ms=100.0,
    min_duration_ms=50.0,
    max_duration_ms=3000.0,
    refractory_ms=200.0,
    min_speed=0.03,
    min_distance=0.008,
)

#: Bias-blind, because every capture here is fed raw readings -- these steps
#: are here to measure the offsets, so stillness cannot be judged as though
#: they were already known. Tolerances are spreads across the window rather
#: than distances from 1 g and zero, roughly ten times the part's own noise:
#: a hand resting on the table shows up, the board's own zero-rate offset,
#: however large, does not.
STILL_WINDOW = 25
STILL_ACCEL_TOLERANCE_G = 0.02
STILL_GYRO_TOLERANCE_DPS = 1.5

GYRO_TARGET_SAMPLES = 400
#: A rest capture whose spread exceeds this was not actually at rest for the
#: whole window -- something knocked it -- and is thrown away rather than
#: averaged into a bias that would be wrong for exactly as long as the board
#: keeps that offset.
GYRO_MAX_SPREAD_DPS = 2.0

ACCEL_SAMPLES_PER_POSITION = 150

#: Mag coverage score needed before a fit is attempted: the smaller of octant
#: coverage and a sample-count floor, so neither a fast, narrow spin nor a
#: slow, thorough one alone can call the step done.
MAG_SCORE_TARGET = 1.0
MAG_SCORE_WARN = 0.4

#: Verify step thresholds -- physics that must hold regardless of what the
#: three capture steps measured, so a wrong step shows up here rather than
#: silently playing wrong.
VERIFY_MAX_ROTATION_DPS = 0.5
VERIFY_GRAVITY_TOLERANCE_G = 0.02
VERIFY_FIELD_MIN_UT = 25.0
VERIFY_FIELD_MAX_UT = 65.0

#: Where a board's own working calibration lives between sessions, one file
#: per label (a board's hand, or "solo") so two boards calibrating at once
#: cannot clobber each other's file. Sibling to calibration.AUTOSAVE_PATH,
#: which is the single-board dashboard's equivalent.
_AUTOSAVE_DIR = Path.home() / ".bbda"


def _autosave_path(label: str) -> Path:
    return _AUTOSAVE_DIR / f"calibration_{label or 'solo'}.json"


def _load_baseline(label: str) -> Calibration:
    """The board's last saved calibration, or a fresh one if there is none."""
    try:
        return Calibration.load(_autosave_path(label))
    except (OSError, ValueError):
        return Calibration()


class CalSequence:
    """One board's calibration run: feed it samples, read its state back.

    No I/O of its own -- it neither owns a link nor talks to the game
    directly. :mod:`bbda.service` feeds it samples from the normal sample
    path, forwards ``cal_advance``/``cal_cancel`` to it, polls
    :meth:`state` at ~10 Hz to emit as a ``cal_state`` record, and reads
    :attr:`cal` once :attr:`step` reaches ``"check"`` to build the commands
    ``cal_save`` sends to the board.
    """

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.step = "rest"
        self.cal = Calibration()

        self._still = StationaryDetector(
            window=STILL_WINDOW, bias_blind=True,
            accel_tolerance_g=STILL_ACCEL_TOLERANCE_G,
            gyro_tolerance_dps=STILL_GYRO_TOLERANCE_DPS,
        )
        self._gyro = GyroBiasCollector(target=GYRO_TARGET_SAMPLES)
        self._accel = AccelSixPointCollector(samples_per_position=ACCEL_SAMPLES_PER_POSITION)
        self._accel_active_name: str | None = None
        self._mag = MagCollector()

        # Orientation step.
        self._moves: dict[str, np.ndarray] = {}
        self._move_index = 0
        self._move_detector = QuickMoveDetector(**SLIDE_DETECTOR)
        self._gravity_ref: np.ndarray | None = None
        self._gravity_track = np.zeros(3)
        self._gravity_buffer: list[np.ndarray] = []
        self._gravity_fresh = False
        self._settle_wait = 0
        self._turn_deg = 0.0
        self._turn_peak = 0.0
        self._last_sample_t: float | None = None
        self._mount_known = False

        # Check step. `verify` is public: the service reads it once `step`
        # reaches "check" to decide what `cal_save` reports as accepted.
        self.verify: list[dict] = []
        self._verify_done = False

        #: Set once the sequence has ended, one way or another. ``result``
        #: is a ``cal_done`` payload (minus ``type``) once this is True.
        self.finished = False
        self.result: dict | None = None

    # ------------------------------------------------------------------
    # Driving it
    # ------------------------------------------------------------------
    def start(self, kind: str) -> None:
        """Begin a run. ``kind`` is ``"full"`` or ``"quick"``."""
        self.cal = _load_baseline(self.label)
        if kind == "quick":
            self.step = "orientation"
            self._enter_orientation()
        else:
            self.step = "rest"
        self._still.reset()

    def cancel(self) -> None:
        self.finished = True
        self.result = {"accepted": False, "detail": "cancelled"}

    def advance(self) -> None:
        """The player pressed Continue.

        Only the orientation step waits on this: after one slide the frame is
        already a complete answer, and advancing skips the optional second.
        Every other step decides for itself when it is done, so this is a
        no-op anywhere else -- there is nothing it would be advancing past.
        """
        if self.step == "orientation" and self._mount_known:
            self._finish_orientation()

    def feed(self, sample: Sample) -> None:
        """One raw sample. Raw is essential -- these steps measure the
        offsets a corrected reading would already have removed."""
        if self.finished:
            return
        still = self._still.update(sample.accel, sample.gyro)
        ready = self._still.ready

        if self.step == "rest":
            self._do_rest(sample, still, ready)
        elif self.step == "six_sides":
            self._do_six_sides(sample, still, ready)
        elif self.step == "wave":
            self._do_wave(sample)
        elif self.step == "orientation":
            self._do_orientation(sample)
        elif self.step == "check":
            self._do_check(sample, still, ready)

    def state(self) -> dict:
        """The current step, as a ``cal_state`` record (minus ``v``/``hand``,
        which :meth:`GameBridge._emit` adds)."""
        prompt, progress, quality = {
            "rest": self._rest_state,
            "six_sides": self._six_sides_state,
            "wave": self._wave_state,
            "orientation": self._orientation_state,
            "check": self._check_state,
        }[self.step]()
        return protocol.cal_state(step=self.step, progress=progress,
                                   prompt=prompt, quality=quality)

    # ------------------------------------------------------------------
    # Rest: gyroscope bias
    # ------------------------------------------------------------------
    def _rest_state(self) -> tuple[str, float, dict]:
        progress = self._gyro.count / self._gyro.target
        prompt = ("Set the board flat on the table and take your hands off it."
                  if progress < 1.0 else "Holding still -- measuring...")
        return prompt, progress, {"samples": self._gyro.count,
                                   "target": self._gyro.target}

    def _do_rest(self, sample: Sample, still: bool, ready: bool) -> None:
        if not (still and ready):
            if self._gyro.count:
                self._gyro = GyroBiasCollector(target=GYRO_TARGET_SAMPLES)
            return
        self._gyro.add(sample.gyro)
        if not self._gyro.done:
            return

        bias, spread = self._gyro.result()
        if float(np.max(spread)) > GYRO_MAX_SPREAD_DPS:
            # Something knocked it mid-capture -- start over rather than
            # average a partly-moving board into the bias.
            self._gyro = GyroBiasCollector(target=GYRO_TARGET_SAMPLES)
            return

        self.cal.gyro_bias = bias
        self._autosave()
        self._enter("six_sides")

    # ------------------------------------------------------------------
    # Six sides: accelerometer bias and gain
    # ------------------------------------------------------------------
    def _six_sides_state(self) -> tuple[str, float, dict]:
        done = len(self._accel.captured)
        progress = done / len(SIX_POSITIONS)
        if self._accel.capturing:
            prompt = "Hold it steady -- capturing this side..."
        else:
            prompt = ("Rest the board on a side you have not done yet, and "
                      "hold it steady.")
        quality = {name: (f"{name} up" in self._accel.captured)
                   for name, *_ in SIX_POSITIONS}
        return prompt, progress, quality

    def _do_six_sides(self, sample: Sample, still: bool, ready: bool) -> None:
        if self._accel.capturing:
            if not still:
                # Moved mid-capture: the partial average is worthless: restart
                # the same position rather than record a smeared reading.
                self._accel.start(self._accel_active_name)
                return
            self._accel.add(sample.accel)
            if self._accel.capturing:
                return
            if self._accel.complete:
                self._finish_six_sides()
            return

        found = FlipDetector.face_of(sample.accel)
        if not (still and ready) or found is None:
            return
        face, _index, _sign = found
        name = f"{face} up"
        if name in self._accel.captured:
            return       # already done this one; wait for a different side
        self._accel_active_name = name
        self._accel.start(name)

    def _finish_six_sides(self) -> None:
        bias, scale = self._accel.result()
        self.cal.accel_bias = bias
        self.cal.accel_scale = scale
        self._autosave()
        self._enter("wave")

    # ------------------------------------------------------------------
    # Wave it around: magnetometer hard/soft iron
    # ------------------------------------------------------------------
    def _wave_state(self) -> tuple[str, float, dict]:
        coverage = self._mag.coverage()
        score = min(coverage, self._mag.count / 250.0)
        if score < MAG_SCORE_WARN:
            prompt = "Keep turning the board -- try rolling it over too."
        elif score < MAG_SCORE_TARGET:
            prompt = "Good -- keep going, find new angles."
        else:
            prompt = "Checking the fit..."
        return prompt, min(1.0, score), {"coverage": coverage,
                                          "samples": self._mag.count}

    def _do_wave(self, sample: Sample) -> None:
        if not sample.mag_fresh:
            return
        self._mag.add(sample.mag)
        coverage = self._mag.coverage()
        score = min(coverage, self._mag.count / 250.0)
        if score < MAG_SCORE_TARGET:
            return
        result = self._mag.fit()
        if not result.ok:
            return      # not enough variety yet; keep collecting
        self.cal.mag_bias = result.bias
        self.cal.mag_soft = result.soft
        self._autosave()
        self._enter("orientation")

    # ------------------------------------------------------------------
    # Which way is which: mounting orientation
    # ------------------------------------------------------------------
    def _enter_orientation(self) -> None:
        self._moves = {}
        self._move_index = 0
        self._move_detector.reset()
        self._gravity_ref = None
        self._gravity_track = np.zeros(3)
        self._gravity_buffer = []
        self._gravity_fresh = False
        self._settle_wait = 0
        self._turn_deg = 0.0
        self._turn_peak = 0.0
        self._last_sample_t = None
        self._mount_known = False
        # Board axes throughout: the mounting rotation is what this step is
        # solving for, so applying a stale one here would feed the answer
        # back into the question.
        self.cal.mount = np.eye(3)

    def _orientation_state(self) -> tuple[str, float, dict]:
        found_up = self._gravity_ref is not None
        steps_done = (1 if found_up else 0) + len(self._moves)
        progress = min(1.0, steps_done / 2.0)
        if not found_up:
            prompt = "Lay the board flat on the table and let go for a moment."
        elif self._move_index < len(MOVE_SEQUENCE):
            prompt = MOVE_SEQUENCE[self._move_index][1]
        else:
            prompt = "Done -- send cal_advance, or cal_save to finish."
        quality = {"up_found": found_up,
                   "moves_done": list(self._moves.keys()),
                   "moves_total": len(MOVE_SEQUENCE),
                   "can_finish": self._mount_known}
        return prompt, progress, quality

    def _do_orientation(self, sample: Sample) -> None:
        dt = 0.0
        if self._last_sample_t is not None:
            dt = sample.t - self._last_sample_t
        self._last_sample_t = sample.t

        accel = self.cal.correct_accel(sample.accel)
        gyro = self.cal.correct_gyro(sample.gyro)

        if not self._gravity_fresh:
            if self._collect_gravity(accel):
                self._gravity_fresh = True
                self._settle_wait = 0
                return
            self._settle_wait += 1
            return
        if dt <= 0:
            return

        # Follow gravity through the slide with the gyroscope, rather than
        # holding the resting value fixed -- tilt leakage is the largest
        # error here, and integrating the tracked vector's rotation costs
        # nothing in drift over the second or so one slide lasts.
        self._gravity_track -= np.cross(np.radians(gyro), self._gravity_track) * dt
        length = float(np.linalg.norm(self._gravity_track))
        if length > 1e-6:
            self._gravity_track *= float(np.linalg.norm(self._gravity_ref)) / length

        linear = (accel - self._gravity_track) * G_MS2

        was_active = self._move_detector.active
        move = self._move_detector.update(sample.t, linear, dt)
        if self._move_detector.active:
            if not was_active:
                self._turn_deg = 0.0
                self._turn_peak = 0.0
            up = self._gravity_ref / float(np.linalg.norm(self._gravity_ref))
            self._turn_deg += float(np.dot(gyro, up)) * dt
            self._turn_peak = max(self._turn_peak, abs(self._turn_deg))

        if move is None:
            return

        if self._turn_peak > MOVE_TURN_LIMIT_DEG:
            # Swivelled rather than pushed: the reference is stale for
            # wherever the board ended up, so it has to be taken again.
            self._gravity_fresh = False
            return

        if self._move_index >= len(MOVE_SEQUENCE):
            return
        name = MOVE_SEQUENCE[self._move_index][0]
        self._moves[name] = move.direction
        self._move_index += 1
        self._gravity_fresh = False
        self._update_frame()

    def _collect_gravity(self, accel: np.ndarray) -> bool:
        buffer = self._gravity_buffer
        if buffer and float(np.linalg.norm(accel - buffer[0])) > GRAVITY_SPREAD_G:
            buffer.clear()
        buffer.append(np.asarray(accel, dtype=float).copy())
        if len(buffer) < GRAVITY_SAMPLES:
            return False
        self._gravity_ref = np.mean(buffer, axis=0)
        self._gravity_track = self._gravity_ref.copy()
        buffer.clear()
        self._move_detector.reset()
        return True

    def _update_frame(self) -> None:
        result = solve_frame_from_gravity_and_moves(self._gravity_ref, self._moves)
        if not result.ok:
            # Slides disagreed; the player can redo the step from the top by
            # cancelling and restarting, or try the second slide anyway.
            return
        self.cal.mount = result.snapped
        self._mount_known = True
        self._autosave()
        if self._move_index >= len(MOVE_SEQUENCE):
            self._finish_orientation()

    def _finish_orientation(self) -> None:
        self._enter("check")

    # ------------------------------------------------------------------
    # Check: verify against physics that must hold
    # ------------------------------------------------------------------
    def _check_state(self) -> tuple[str, float, dict]:
        if not self._verify_done:
            return "Put the board down flat and let go.", 0.0, {"checks": []}
        passed = sum(1 for c in self.verify if c["ok"])
        prompt = (f"All {len(self.verify)} checks passed -- send cal_save "
                  f"to finish" if passed == len(self.verify) else
                  f"{passed} of {len(self.verify)} passed -- cal_save will "
                  f"still write it, or cancel and redo the step named below")
        return prompt, 1.0, {"checks": self.verify}

    def _do_check(self, sample: Sample, still: bool, ready: bool) -> None:
        if self._verify_done or not (still and ready):
            return
        accel = self.cal.apply_accel(sample.accel)
        gyro = self.cal.apply_gyro(sample.gyro)
        mag = self.cal.apply_mag(sample.mag)

        gravity = float(np.linalg.norm(accel))
        rotation = float(np.linalg.norm(gyro))
        field = float(np.linalg.norm(mag))

        self.verify = [
            {"name": "no rotation while still", "ok": rotation < VERIFY_MAX_ROTATION_DPS,
             "value": round(rotation, 3)},
            {"name": "gravity measures 1.00 g",
             "ok": abs(gravity - 1.0) < VERIFY_GRAVITY_TOLERANCE_G,
             "value": round(gravity, 3)},
            {"name": "magnetic field in the normal range",
             "ok": VERIFY_FIELD_MIN_UT <= field <= VERIFY_FIELD_MAX_UT,
             "value": round(field, 1)},
            {"name": "axes have been named", "ok": self._mount_known,
             "value": alignment_name(self.cal.mount) if self._mount_known else "unknown"},
        ]
        self._verify_done = True

    # ------------------------------------------------------------------
    def _enter(self, step: str) -> None:
        self.step = step
        self._still.reset()
        if step == "orientation":
            self._enter_orientation()
        elif step == "check":
            self.verify = []
            self._verify_done = False

    def _autosave(self) -> None:
        try:
            self.cal.save(_autosave_path(self.label))
        except OSError:
            pass      # a failed autosave costs nothing already written to disk
