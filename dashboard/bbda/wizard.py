"""Easy mode: a guided calibration screen for someone who has never done one.

The expert panel asks you to know what a hard-iron offset is, which of the six
board faces you are holding up, and when a fit is good enough. This screen
asks none of that. It works out what it can for itself:

* it detects when the board is actually still, and only captures then, so
  "hold it steady" is enforced rather than requested;
* it recognises which face is pointing up from gravity, so the six-position
  step becomes "put it on a side you have not done yet" instead of a puzzle
  about axis signs;
* it watches magnetometer coverage and stops when the fit will succeed,
  instead of leaving you to guess;
* it learns which way round the board is mounted by reading which way is up
  off gravity and asking you to shove the board along the table, rather than
  asking you to work out which of 24 axis mappings describes your board;
* it finishes by checking its own work against physics that must hold -- the
  board is not accelerating, gravity is 1 g, Earth's field is 25-65 uT -- and
  says plainly whether the result is good.

Every instruction is a physical action. No jargon reaches the screen.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .calibration import (
    AUTOSAVE_PATH,
    SIX_POSITIONS,
    AccelSixPointCollector,
    Calibration,
    GyroBiasCollector,
    MagCollector,
    alignment_name,
    solve_frame_from_gravity_and_moves,
)
from .motion import G_MS2, FlipDetector, QuickMoveDetector, StationaryDetector

# Plain-language name for each board face, so the checklist never shows an
# axis letter to someone who does not care what an axis is.
FACE_WORDS = {
    "+Z": "lying flat, chip side up",
    "-Z": "lying flat, chip side down",
    "+X": "on its edge, one end up",
    "-X": "on its edge, other end up",
    "+Y": "on its long edge, one side up",
    "-Y": "on its long edge, other side up",
}

# The slides the orientation step asks for, in order: the checklist word, the
# prompt, and the hint under it. Two, and the second is optional -- gravity
# already supplies the vertical axis, so one horizontal slide completes the
# answer and the other only confirms it. See
# calibration.solve_frame_from_gravity_and_moves for why that is enough.
MOVE_SEQUENCE = (
    ("forward", "slide away from you",
     "Shove the board AWAY from you, then let it stop.",
     "Flat across the table, roughly a hand's width. It does not need to be "
     "straight, fast or neat — anywhere in that general direction will do."),
    ("right", "slide to your right",
     "Now shove the board to your RIGHT, then let it stop.",
     "Same again, sideways. This only double-checks the first slide, so press "
     "Next instead if you would rather skip it."),
)

# How much the board may turn about the vertical during a slide before the
# answer stops being trustworthy. Turning mid-slide smears the very axes the
# step is measuring; 35 degrees is roughly where the snap to the nearest of
# the 24 mappings starts to be at risk.
#
# This is an *angle*, not a rate, and deliberately so: a rate limit rejects a
# brief wobble that nets no rotation at all, which is most of what a hand does
# to a board being shoved across a desk. Integrating the rate about the
# gravity direction measures the thing that actually corrupts the result.
MOVE_TURN_LIMIT_DEG = 35.0

# Thresholds for the slide detector, all well below the library defaults. The
# defaults are tuned for gestures that must not fire by accident during normal
# handling; here the user has been asked for one specific movement and is
# waiting for it to register, so missing a real slide is much worse than
# accepting a feeble one.
#
# 0.35 m/s^2 of trigger -- about 0.035 g -- is what a 10 cm slide taken over a
# leisurely 1.2 seconds produces, and it clears the sensor's own ~0.002 g of
# noise by a wide margin because the baseline it is measured against is a
# gravity reading taken moments earlier from the same still board. Together
# with the floors below that covers everything from a 3 cm flick to a 20 cm
# shove taken over most of two seconds. A false trigger is cheap in any case:
# the speed and distance floors discard it before it can name an axis.
SLIDE_DETECTOR = dict(
    sector_map=None,        # discovering the frame, so accept any direction
    on_threshold_ms2=0.35,
    off_threshold_ms2=0.15,
    quiet_ms=250.0,         # a slide is nearly still around its own midpoint
    rearm_ms=100.0,
    min_duration_ms=50.0,
    max_duration_ms=3000.0,  # no penalty for taking it slowly
    refractory_ms=200.0,
    min_speed=0.03,
    min_distance=0.008,     # under a centimetre still names an axis
)

# Samples averaged into one gravity reading, and how far apart the readings in
# that run may be for it to count as a run.
#
# The spread test is doing real work rather than belt-and-braces. StationaryDetector
# watches the accelerometer's *magnitude*, and sliding a level board horizontally
# barely changes that -- 0.08 g sideways added to 1 g down comes to 1.003 g -- so
# it reports "still" throughout a slide across a table. Anything that trusts it
# to fence off the movement will quietly average the beginning of the push into
# the reference it is meant to be measured against.
GRAVITY_SAMPLES = 60
GRAVITY_SPREAD_G = 0.015

# How long re-measuring may take before the screen mentions it, in samples.
SETTLE_NAG_SAMPLES = 400

STEPS = ["Get ready", "Hold still", "Six sides", "Wave it around",
         "Which way is which", "Check", "Save"]


class EasyModeWizard(QWidget):
    """Full-window guided calibration."""

    finished = Signal()

    def __init__(self, dashboard, parent=None) -> None:
        super().__init__(parent)
        self.dash = dashboard

        self._step = 0
        self._substate = ""
        # Bias-blind, because every capture on this screen is fed raw
        # readings -- it is here to measure the offsets, so it cannot require
        # them to be small already. The tolerances are spreads across the
        # window rather than distances from 1 g and zero: roughly ten times
        # the part's own noise, so a hand resting on the table shows up but
        # the board's own zero-rate offset, however large, does not.
        self._still = StationaryDetector(
            window=25,
            bias_blind=True,
            accel_tolerance_g=0.02,
            gyro_tolerance_dps=1.5,
        )
        self._gyro = GyroBiasCollector(target=400)
        self._accel = AccelSixPointCollector(samples_per_position=150)
        self._mag = MagCollector()
        self._mag_result = None
        self._verify: list[tuple[str, bool, str]] = []
        self._saved = False
        # Whether the mounting orientation is an answer rather than the
        # identity default. Separate from the frame fit because loading a file
        # supplies one without any slides being measured.
        self._mount_known = False

        # Orientation step.
        self._moves: dict[str, np.ndarray] = {}
        self._move_index = 0
        self._move_detector = QuickMoveDetector(**SLIDE_DETECTOR)
        self._gravity_ref: np.ndarray | None = None
        self._gravity_track = np.zeros(3)   # ref carried through the slide
        self._gravity_buffer: list[np.ndarray] = []
        self._gravity_fresh = False  # re-measured since the last slide?
        self._settle_wait = 0
        self._up_announced = False
        self._turn_deg = 0.0        # signed turn about vertical, this slide
        self._turn_peak = 0.0       # the largest that reached, either way
        self._frame_result = None
        self._last_sample_t: float | None = None

        self._build_ui()
        self._show_step(0)

    # ==================================================================
    # Layout
    # ==================================================================
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- header -----------------------------------------------------
        header = QWidget()
        header.setObjectName("wizHeader")
        header.setStyleSheet(
            f"QWidget#wizHeader {{ background: {theme.PANEL};"
            f" border-bottom: 1px solid {theme.BORDER_SOLID}; }}"
            f"QWidget#wizHeader QLabel {{ background: transparent; }}"
        )
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(18, 12, 18, 12)

        title = QLabel("Easy calibration")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        head_row.addWidget(title)
        head_row.addSpacing(24)

        self._chips: list[QLabel] = []
        for index, name in enumerate(STEPS):
            chip = QLabel(f"{index + 1}. {name}")
            chip.setStyleSheet(f"color: {theme.INK_MUTED};")
            head_row.addWidget(chip)
            if index < len(STEPS) - 1:
                arrow = QLabel("›")
                arrow.setStyleSheet(f"color: {theme.BASELINE};")
                head_row.addWidget(arrow)
            self._chips.append(chip)

        head_row.addStretch(1)
        self.exit_button = QPushButton("Leave easy mode")
        self.exit_button.clicked.connect(self.finished.emit)
        head_row.addWidget(self.exit_button)
        outer.addWidget(header)

        # Every step here is a wait for readings, so a board that stops sending
        # looks exactly like a board being held beautifully still: the progress
        # bar simply stops, and nothing on screen says why. This is what says
        # why. It sits above the instructions rather than in the feedback strip
        # because the strip is mid-sentence about the step in progress.
        self.alert = QLabel()
        self.alert.setWordWrap(True)
        self.alert.setVisible(False)
        self.alert.setStyleSheet(
            f"color: {theme.STATUS['critical']}; font-weight: 600;"
            f" border: 1px solid {theme.STATUS['critical']}; border-radius: 6px;"
            " padding: 8px 12px; margin: 10px 40px 0 40px;"
        )
        outer.addWidget(self.alert)

        # -- body -------------------------------------------------------
        body_outer = QWidget()
        body_row = QHBoxLayout(body_outer)
        body_row.setContentsMargins(40, 28, 40, 20)
        body_row.addStretch(1)

        body = QWidget()
        body.setFixedWidth(860)
        body_row.addWidget(body, 0, Qt.AlignTop)
        body_row.addStretch(1)

        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        self.heading = QLabel()
        self.heading.setStyleSheet("font-size: 25px; font-weight: 600;")
        self.heading.setWordWrap(True)
        body_layout.addWidget(self.heading)

        self.instruction = QLabel()
        self.instruction.setStyleSheet(
            f"font-size: 15px; color: {theme.INK_SECONDARY};"
        )
        self.instruction.setWordWrap(True)
        body_layout.addWidget(self.instruction)

        # Live feedback strip: the one line that changes as they move.
        self.feedback = QLabel()
        self.feedback.setAlignment(Qt.AlignCenter)
        self.feedback.setMinimumHeight(58)
        self.feedback.setStyleSheet(
            f"font-size: 18px; font-weight: 600; border: 1px solid {theme.BORDER_SOLID};"
            f" border-radius: 8px; background: {theme.SURFACE};"
        )
        body_layout.addWidget(self.feedback)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        body_layout.addWidget(self.progress)

        # Six-face checklist, only shown during that step.
        self.checklist = QWidget()
        grid = QGridLayout(self.checklist)
        grid.setContentsMargins(0, 4, 0, 4)
        self._face_labels: dict[str, QLabel] = {}
        for index, (face, words) in enumerate(FACE_WORDS.items()):
            item = QLabel(f"○  {words}")
            item.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 14px;")
            grid.addWidget(item, index // 2, index % 2)
            self._face_labels[face] = item
        body_layout.addWidget(self.checklist)

        # Movement checklist, only shown during the orientation step. "up" is
        # in it even though nothing is asked for: it is the one the board
        # works out on its own, and showing it tick itself off is what makes
        # that visible rather than mysterious.
        self.move_checklist = QWidget()
        move_grid = QGridLayout(self.move_checklist)
        move_grid.setContentsMargins(0, 4, 0, 4)
        self._move_labels: dict[str, QLabel] = {}
        rows = [("up", "which way is up")]
        rows += [(name, words) for name, words, _p, _h in MOVE_SEQUENCE]
        for index, (name, words) in enumerate(rows):
            item = QLabel(f"○  {words}")
            item.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 14px;")
            move_grid.addWidget(item, index // 3, index % 3)
            self._move_labels[name] = item
        body_layout.addWidget(self.move_checklist)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        # The verify step prints four findings plus remedies; reserve the room
        # so they cannot be cut off half way through a sentence.
        self.detail.setMinimumHeight(124)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail.setStyleSheet(f"color: {theme.INK_MUTED};")
        body_layout.addWidget(self.detail)

        outer.addWidget(body_outer, 1)

        # -- footer -----------------------------------------------------
        footer = QFrame()
        footer.setObjectName("wizFooter")
        footer.setStyleSheet(
            f"QFrame#wizFooter {{ border-top: 1px solid {theme.BORDER_SOLID}; }}"
        )
        foot_row = QHBoxLayout(footer)
        foot_row.setContentsMargins(40, 12, 40, 12)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._back)
        self.redo_button = QPushButton("Redo this step")
        self.redo_button.clicked.connect(self._redo)
        # Loading is offered before anything has been measured and saving only
        # once everything has, because those are the two moments where a file
        # is the whole answer rather than half of one.
        self.load_file_button = QPushButton("Load a saved file…")
        self.load_file_button.clicked.connect(self._load_from_file)
        self.save_file_button = QPushButton("Save to a file…")
        self.save_file_button.clicked.connect(self._save_to_file)
        self.next_button = QPushButton("Start")
        self.next_button.setObjectName("primary")
        self.next_button.setMinimumWidth(170)
        self.next_button.clicked.connect(self._next)

        foot_row.addWidget(self.back_button)
        foot_row.addWidget(self.redo_button)
        foot_row.addWidget(self.load_file_button)
        foot_row.addStretch(1)
        foot_row.addWidget(self.save_file_button)
        foot_row.addWidget(self.next_button)
        outer.addWidget(footer)

    # ==================================================================
    # Step control
    # ==================================================================
    def set_link_alert(self, message: str | None) -> None:
        """Show the dashboard's link warning here too, or clear it."""
        self.alert.setText(message or "")
        self.alert.setVisible(bool(message))

    def restart(self) -> None:
        self._gyro = GyroBiasCollector(target=400)
        self._accel = AccelSixPointCollector(samples_per_position=150)
        self._mag = MagCollector()
        self._mag_result = None
        self._saved = False
        self._reset_orientation()
        self._show_step(0)

    def _reset_orientation(self) -> None:
        self._moves = {}
        self._move_index = 0
        self._move_detector.reset()
        self._gravity_ref = None
        self._gravity_track = np.zeros(3)
        self._gravity_buffer = []
        self._gravity_fresh = False
        self._settle_wait = 0
        self._up_announced = False
        self._turn_deg = 0.0
        self._turn_peak = 0.0
        self._frame_result = None
        self._last_sample_t = None
        self._mount_known = False

    def _show_step(self, step: int) -> None:
        self._step = step
        self._substate = ""
        self._still.reset()
        self.progress.setValue(0)
        self.checklist.setVisible(step == 2)
        self.move_checklist.setVisible(step == 4)
        self.redo_button.setVisible(step in (1, 2, 3, 4))
        self.load_file_button.setVisible(step == 0)
        self.save_file_button.setVisible(step >= 6)
        self.back_button.setEnabled(step > 0)
        self.next_button.setEnabled(step == 0)
        self.next_button.setText("Start" if step == 0 else "Next")
        self.detail.setText("")

        for index, chip in enumerate(self._chips):
            if index < step:
                chip.setStyleSheet(f"color: {theme.STATUS['good']};")
            elif index == step:
                chip.setStyleSheet(
                    f"color: {theme.INK_PRIMARY}; font-weight: 600;"
                )
            else:
                chip.setStyleSheet(f"color: {theme.INK_MUTED};")

        if step == 0:
            self.heading.setText("Let's calibrate your board")
            self.instruction.setText(
                "This takes about three minutes and needs no tools.\n\n"
                "Before you start:\n"
                "  •  Put the board on a wooden or plastic table, not metal.\n"
                "  •  Move phones, laptops, speakers and headphones about an "
                "arm's length away. They contain magnets that will spoil the result.\n"
                "  •  Leave the board plugged in and powered for a minute first, "
                "so it is at its normal working temperature.\n\n"
                "You will put the board down, turn it onto each of its six sides, "
                "wave it around, then slide it in a few directions. That is all.\n\n"
                "Leave yourself a clear space on the table about two hand-widths "
                "across — the last step asks you to move the board around in it.\n\n"
                "Nothing here can be lost by stopping half way: each step is "
                "written to a file on this computer the moment it finishes, and "
                "read back automatically next time. 'Load a saved file' is for "
                "putting back a different one — it skips to the checks, so you "
                "can see whether it still fits this board before trusting it."
            )
            self.feedback.setText("Ready when you are")
            self._set_feedback_colour(theme.INK_MUTED)

        elif step == 1:
            self.heading.setText("Step 1 of 6 — put it down and let go")
            self.instruction.setText(
                "Set the board flat on the table and take your hands off it.\n\n"
                "Do not touch the table. This measures the tiny drift the "
                "board reports when it is definitely not moving, so any nudge "
                "spoils it. It starts by itself once everything is still, and "
                "takes about four seconds."
            )
            self.feedback.setText("Waiting for the board to settle…")
            self._set_feedback_colour(theme.INK_MUTED)

        elif step == 2:
            self.heading.setText("Step 2 of 6 — rest it on each of its six sides")
            self.instruction.setText(
                "Put the board down on one side and hold it steady until that "
                "line below turns green. Then turn it onto a side you have not "
                "done yet, and repeat.\n\n"
                "You do not need to work out which side is which — it "
                "recognises each one. Rest each side against something solid "
                "rather than holding it in mid-air."
            )
            self.feedback.setText("Rest the board on any side")
            self._set_feedback_colour(theme.INK_MUTED)
            self._refresh_checklist()

        elif step == 3:
            self.heading.setText("Step 3 of 6 — wave it around")
            self.instruction.setText(
                "Pick the board up and turn it slowly in as many directions as "
                "you can. Roll it over, spin it, tip it on each corner — "
                "like slowly wiping the inside of a ball.\n\n"
                "Keep the cable clear of the board and stay away from anything "
                "metal. The bar fills as you cover new directions, and this "
                "step finishes on its own."
            )
            self.feedback.setText("Start turning the board")
            self._set_feedback_colour(theme.INK_MUTED)

        elif step == 4:
            self.heading.setText("Step 4 of 6 — which way is which")
            self.instruction.setText(
                "All this step does is name the board's axes for you: which "
                "one points away from you, which one points left, which one "
                "points up.\n\n"
                "Which way is up it reads off gravity by itself, as soon as "
                "the board is sitting flat and still — nothing to do. For the "
                "rest, lay the board on the table the way you mean to use it, "
                "then shove it away from you and let it stop.\n\n"
                "It does not have to be neat. A rough shove in roughly the "
                "right direction is enough, because the answer is one of 24 "
                "fixed mappings and anything within 45° of straight lands on "
                "the same one. Just avoid turning the board as you push it. "
                "A second, sideways slide is offered afterwards to "
                "double-check the first, and you can skip it."
            )
            self._reset_orientation()
            self.dash.cal.mount = np.eye(3)
            self._refresh_move_checklist()
            self.feedback.setText("Lay the board flat on the table and let go")
            self._set_feedback_colour(theme.INK_MUTED)

        elif step == 5:
            self.heading.setText("Step 5 of 6 — checking the result")
            self.instruction.setText(
                "Put the board back down flat and let go.\n\n"
                "Three things must now be true: a board sitting still should "
                "report no rotation, gravity should measure exactly 1 g, and "
                "the Earth's magnetic field should come out between 25 and 65 "
                "microtesla. If any of them is off, the step that caused it is "
                "named so you can redo just that one."
            )
            self.feedback.setText("Put the board down flat…")
            self._set_feedback_colour(theme.INK_MUTED)

        elif step == 6:
            self.heading.setText("Step 6 of 6 — save it")
            self.instruction.setText(
                "Saving to the board writes the correction into the board "
                "itself, so it stays there after you unplug it and applies even "
                "without this program running.\n\n"
                "Which way round the board is mounted is not saved to the "
                "board: it describes how you use it, not how it behaves, and "
                "writing it there would turn the board's own printed readings "
                "away from the axis names on the silkscreen.\n\n"
                f"Everything measured is already on this computer, in\n"
                f"{AUTOSAVE_PATH}\n"
                "— written after each step as it finished, and loaded again by "
                "itself next time. Nothing needs pressing for that.\n\n"
                "'Save to a file' is for a named copy somewhere of your own "
                "choosing: one per board, or one to keep before trying a fresh "
                "calibration."
            )
            self.next_button.setText("Save to the board")
            self.next_button.setEnabled(self.dash.link.connected)
            if not self.dash.link.connected:
                self.feedback.setText("Not connected to the board")
                self._set_feedback_colour(theme.STATUS["critical"])
            else:
                self.feedback.setText("Ready to save")
                self._set_feedback_colour(theme.INK_SECONDARY)

        elif step == 7:
            self.heading.setText("All done")
            self.instruction.setText(
                "The board is calibrated and the result is stored on it.\n\n"
                "A copy is on this computer too, kept automatically and loaded "
                "again next time. 'Save to a file' is still there if you want a "
                "second one somewhere of your own choosing.\n\n"
                "Leave easy mode to see the live view. If you ever move the "
                "board somewhere with different metal nearby — a new desk, "
                "a case, a vehicle — run this again."
            )
            self.feedback.setText("Calibration saved")
            self._set_feedback_colour(theme.STATUS["good"])
            self.next_button.setText("Finish")
            self.next_button.setEnabled(True)
            self.redo_button.setVisible(False)

    def _set_feedback_colour(self, colour: str) -> None:
        self.feedback.setStyleSheet(
            f"font-size: 18px; font-weight: 600; color: {colour};"
            f" border: 1px solid {colour}; border-radius: 8px;"
            f" background: {theme.SURFACE};"
        )

    def _next(self) -> None:
        if self._step >= 7:
            self.finished.emit()
            return
        if self._step == 6:
            self._save()
            return
        self._show_step(self._step + 1)

    def _back(self) -> None:
        if self._step > 0:
            self._show_step(self._step - 1)

    def _redo(self) -> None:
        if self._step == 1:
            self._gyro = GyroBiasCollector(target=400)
        elif self._step == 2:
            self._accel = AccelSixPointCollector(samples_per_position=150)
        elif self._step == 3:
            self._mag.clear()
            self._mag_result = None
        elif self._step == 4:
            self._reset_orientation()
        self._show_step(self._step)

    # ==================================================================
    # Sample handling -- raw values only
    # ==================================================================
    def on_sample(self, sample) -> None:
        """Feed one RAW sample.

        Raw is essential: a collector fed already-corrected values would fold
        the existing calibration into the new one and quietly double it.
        """
        still = self._still.update(sample.accel, sample.gyro)
        ready = self._still.ready

        if self._step == 1:
            self._do_gyro(sample, still, ready)
        elif self._step == 2:
            self._do_accel(sample, still, ready)
        elif self._step == 3:
            self._do_mag(sample)
        elif self._step == 4:
            self._do_orientation(sample, still, ready)
        elif self._step == 5:
            self._do_verify(sample, still, ready)

    # ------------------------------------------------------------------
    def _do_gyro(self, sample, still: bool, ready: bool) -> None:
        if self._substate == "done":
            return
        if not (still and ready):
            self._gyro = GyroBiasCollector(target=self._gyro.target)
            self.progress.setValue(0)
            self.feedback.setText("Still moving — take your hands off the board")
            self._set_feedback_colour(theme.STATUS["warning"])
            return

        self._gyro.add(sample.gyro)
        self.progress.setValue(int(100 * self._gyro.count / self._gyro.target))
        self.feedback.setText("Holding still — measuring…")
        self._set_feedback_colour(theme.AXIS_COLORS["x"])

        if self._gyro.done:
            bias, spread = self._gyro.result()
            if float(np.max(spread)) > 2.0:
                self._gyro = GyroBiasCollector(target=self._gyro.target)
                self.feedback.setText("Something knocked it — starting again")
                self._set_feedback_colour(theme.STATUS["warning"])
                return
            self.dash.cal.gyro_bias = bias
            self.dash.autosave_calibration("gyroscope bias")
            self._substate = "done"
            self.progress.setValue(100)
            self.feedback.setText("Done — press Next")
            self._set_feedback_colour(theme.STATUS["good"])
            self.detail.setText(
                f"Measured drift: {bias[0]:+.3f}, {bias[1]:+.3f}, {bias[2]:+.3f} "
                "degrees per second. This is now subtracted from every reading."
            )
            self.next_button.setEnabled(True)

    # ------------------------------------------------------------------
    def _do_accel(self, sample, still: bool, ready: bool) -> None:
        if self._accel.capturing:
            self._accel.add(sample.accel)
            self.progress.setValue(int(100 * self._accel.progress))
            if not still:
                # Moved mid-capture: throw the partial away rather than record
                # a smeared average.
                self._accel.start(self._accel_active_name)
                self.feedback.setText("It moved — hold it steady")
                self._set_feedback_colour(theme.STATUS["warning"])
                return
            if not self._accel.capturing:
                self._refresh_checklist()
                self._after_accel_capture()
            return

        found = FlipDetector.face_of(sample.accel)
        if not (still and ready) or found is None:
            self.feedback.setText("Rest it flat on one of its sides and hold steady")
            self._set_feedback_colour(theme.INK_MUTED)
            self.progress.setValue(0)
            return

        face, _index, _sign = found
        name = f"{face} up"
        if name in self._accel.captured:
            self.feedback.setText(
                f"Already done this one ({FACE_WORDS[face]}) — try another side"
            )
            self._set_feedback_colour(theme.INK_MUTED)
            return

        self._accel_active_name = name
        self._accel.start(name)
        self.feedback.setText(f"Got it: {FACE_WORDS[face]} — keep still")
        self._set_feedback_colour(theme.AXIS_COLORS["x"])

    def _after_accel_capture(self) -> None:
        done = len(self._accel.captured)
        if self._accel.complete:
            bias, scale = self._accel.result()
            self.dash.cal.accel_bias = bias
            self.dash.cal.accel_scale = scale
            self.dash.autosave_calibration("accelerometer bias and gain")
            detail = (
                f"Offsets {bias[0]:+.3f}, {bias[1]:+.3f}, {bias[2]:+.3f} g; "
                f"gains {scale[0]:.3f}, {scale[1]:.3f}, {scale[2]:.3f}."
            )
            if self._accel.rejected:
                # Said out loud and in the step that produced it. A refused
                # axis is not a small imperfection to mention later: it means
                # two of the six positions were the same one, and the person
                # who just did them is the only one who can redo them.
                self.feedback.setText("Two sides did not come out right")
                self._set_feedback_colour(theme.STATUS["serious"])
                detail += "\n" + "\n".join(self._accel.rejected)
            else:
                self.feedback.setText("All six sides done — press Next")
                self._set_feedback_colour(theme.STATUS["good"])
            self.detail.setText(detail)
            self.next_button.setEnabled(True)
        else:
            self.feedback.setText(
                f"{done} of 6 done — turn it onto a side you have not done"
            )
            self._set_feedback_colour(theme.STATUS["good"])

    def _refresh_checklist(self) -> None:
        for face, label in self._face_labels.items():
            if f"{face} up" in self._accel.captured:
                label.setText(f"✓  {FACE_WORDS[face]}")
                label.setStyleSheet(
                    f"color: {theme.STATUS['good']}; font-size: 14px;"
                )
            else:
                label.setText(f"○  {FACE_WORDS[face]}")
                label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 14px;")

    # ------------------------------------------------------------------
    def _do_mag(self, sample) -> None:
        if self._substate == "done":
            return
        if not sample.mag_fresh:
            return
        self._mag.add(sample.mag)

        coverage = self._mag.coverage()
        count = self._mag.count
        # Two conditions, because either alone is foolable: coverage says the
        # directions were varied, the count says enough distinct points exist
        # for the fit to be stable.
        score = min(coverage, count / 250.0)
        self.progress.setValue(int(100 * score))

        if score < 0.4:
            self.feedback.setText("Keep turning — try rolling it over too")
            self._set_feedback_colour(theme.AXIS_COLORS["x"])
        elif score < 1.0:
            self.feedback.setText("Good — keep going, find new angles")
            self._set_feedback_colour(theme.AXIS_COLORS["x"])
        else:
            result = self._mag.fit()
            if not result.ok:
                self.feedback.setText("Not enough variety yet — keep turning")
                self._set_feedback_colour(theme.STATUS["warning"])
                return
            self._mag_result = result
            self.dash.cal.mag_bias = result.bias
            self.dash.cal.mag_soft = result.soft
            self.dash.autosave_calibration("magnetometer iron correction")
            self._substate = "done"
            self.progress.setValue(100)
            self.feedback.setText("Done — press Next")
            self._set_feedback_colour(theme.STATUS["good"])
            self.detail.setText(
                f"Magnetic field measured at {result.field_strength:.0f} microtesla, "
                f"with a {result.residual_pct:.1f}% spread over {count} points. "
                "Under about 3% is a good result."
            )
            self.next_button.setEnabled(True)

    # ------------------------------------------------------------------
    # Orientation: learn the frame by watching the board move
    # ------------------------------------------------------------------
    def _refresh_move_checklist(self) -> None:
        found_up = self._gravity_ref is not None
        entries = [("up", "which way is up", found_up, False)]
        for index, (name, words, _p, _h) in enumerate(MOVE_SEQUENCE):
            entries.append(
                (name, words, name in self._moves,
                 found_up and index == self._move_index)
            )

        for name, words, done, current in entries:
            label = self._move_labels[name]
            if done:
                label.setText(f"✓  {words}")
                label.setStyleSheet(f"color: {theme.STATUS['good']}; font-size: 14px;")
            elif current:
                label.setText(f"→  {words}")
                label.setStyleSheet(
                    f"color: {theme.INK_PRIMARY}; font-size: 14px; font-weight: 600;"
                )
            else:
                label.setText(f"○  {words}")
                label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 14px;")

        # The vertical axis is one of the three, and it arrives free.
        steps_done = (1 if found_up else 0) + len(self._moves)
        self.progress.setValue(int(100 * min(1.0, steps_done / 2.0)))

    def _do_orientation(self, sample, still: bool, ready: bool) -> None:
        if self._substate == "done":
            return

        dt = 0.0
        if self._last_sample_t is not None:
            dt = sample.t - self._last_sample_t
        self._last_sample_t = sample.t

        # Board axes throughout: the mounting rotation is what this step is
        # solving for, so applying it here would feed the answer back into the
        # question. cal.mount was set to identity on entering the step.
        accel = self.dash.cal.correct_accel(sample.accel)
        gyro = self.dash.cal.correct_gyro(sample.gyro)

        # Whatever a still board reads is gravity, in board axes -- and that
        # single reading *is* the vertical axis, which is why this step no
        # longer asks anyone to lift the board. It is re-measured before every
        # slide rather than tracked continuously: it has to be the reading
        # from just before *this* slide, so that setting the board down on a
        # slight slope between slides costs nothing, and so that no part of
        # the push can leak into the very baseline the push is measured
        # against.
        if not self._gravity_fresh:
            if still and ready and self._collect_gravity(accel):
                self._gravity_fresh = True
                self._settle_wait = 0
                if not self._up_announced:
                    self._up_announced = True
                    self._announce_up()
                return
            # Re-measuring between slides takes a third of a second and needs
            # no announcement, so the "got it" from the last slide is left on
            # screen. Only say something once the wait has gone on long enough
            # that silence would look like nothing is happening.
            self._settle_wait += 1
            if not self._up_announced:
                self.feedback.setText(
                    "Lay the board flat on the table and let go for a moment…"
                )
                self._set_feedback_colour(theme.INK_MUTED)
            elif self._settle_wait == SETTLE_NAG_SAMPLES:
                self.feedback.setText("Let the board come to rest…")
                self._set_feedback_colour(theme.INK_MUTED)
            return
        if dt <= 0:
            return

        # Follow gravity through the slide with the gyroscope rather than
        # holding the resting value fixed. Tilt leakage is by a wide margin
        # the largest error in this step: rocking the board six degrees while
        # pushing it tips 0.10 g of gravity into the horizontal axes, which is
        # the same size as the push itself, points the same way for the whole
        # stroke, and therefore biases the answer rather than averaging out.
        # A vector fixed in the world turns at -omega as seen from the board,
        # and integrating that over the second or so an event lasts costs
        # nothing in drift.
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
            # Rate about the vertical, integrated: what a slide must not do is
            # end up pointing somewhere else, and a wobble that comes back
            # does not. The unit gravity direction is the vertical in board
            # axes, which is the frame the gyro reports in.
            up = self._gravity_ref / float(np.linalg.norm(self._gravity_ref))
            self._turn_deg += float(np.dot(gyro, up)) * dt
            self._turn_peak = max(self._turn_peak, abs(self._turn_deg))

        if move is None:
            return

        if self._turn_peak > MOVE_TURN_LIMIT_DEG:
            self.feedback.setText(
                f"The board turned about {self._turn_peak:.0f}° as it went — "
                "push it, do not swivel it"
            )
            self._set_feedback_colour(theme.STATUS["warning"])
            # The board is somewhere new and possibly sitting differently, so
            # the reference has to be taken again before the retry.
            self._gravity_fresh = False
            return

        name = MOVE_SEQUENCE[self._move_index][0]
        self._moves[name] = move.direction
        self._move_index += 1
        # The board has come to rest somewhere new, so the reference has to be
        # taken again before the next slide can be measured against it.
        self._gravity_fresh = False
        self._refresh_move_checklist()
        self._update_frame(move)

    def _collect_gravity(self, accel: np.ndarray) -> bool:
        """Average a short run of matching samples; True when one is ready.

        Restarting the run whenever a sample strays from the one it began with
        is what keeps a slide out of the reading, given that the stationary
        detector cannot see a horizontal slide at all (see GRAVITY_SPREAD_G).
        """
        buffer = self._gravity_buffer
        if buffer and float(np.linalg.norm(accel - buffer[0])) > GRAVITY_SPREAD_G:
            buffer.clear()
        buffer.append(np.asarray(accel, dtype=float).copy())
        if len(buffer) < GRAVITY_SAMPLES:
            return False
        self._gravity_ref = np.mean(buffer, axis=0)
        self._gravity_track = self._gravity_ref.copy()
        buffer.clear()
        # The board is demonstrably at rest here, so any velocity the detector
        # is part-way through integrating is stale.
        self._move_detector.reset()
        return True

    def _announce_up(self) -> None:
        """Report the free half of the answer and ask for the first slide.

        The feedback strip always says what to do next, so the finding goes in
        the detail line underneath it rather than taking that slot.
        """
        self._refresh_move_checklist()
        _name, _words, prompt, hint = MOVE_SEQUENCE[self._move_index]
        self.feedback.setText(prompt)
        self._set_feedback_colour(theme.AXIS_COLORS["x"])

        found = FlipDetector.face_of(self._gravity_ref)
        which = f"the board's {found[0]}" if found else "found from gravity"
        self.detail.setText(
            f"Up is {which} — read straight off gravity, nothing to do for "
            f"that one.\n\n{hint}"
        )

    def _update_frame(self, move) -> None:
        """Re-solve with whatever slides exist so far and show the answer.

        Called after *every* slide rather than only after the last, because
        one slide is already a complete answer -- gravity supplied the other
        axis. That is what lets the second slide be optional, and it means a
        person who only wanted the axes named can read them off and press Next
        without doing the whole sequence.
        """
        result = solve_frame_from_gravity_and_moves(self._gravity_ref, self._moves)
        self._frame_result = result
        self.next_button.setEnabled(True)
        last = self._move_index >= len(MOVE_SEQUENCE)
        if last:
            self._substate = "done"

        if not result.ok:
            self.feedback.setText("Those slides did not agree")
            self._set_feedback_colour(theme.STATUS["warning"])
            self.detail.setText(
                result.message
                + "\n\nPress 'Redo this step' to try again, or Next to carry on "
                "with the board's axes used exactly as they come."
            )
            return

        # Snap to one of the 24 exact axis mappings. A board is a rigid piece
        # of fibreglass in a fixed orientation, so the true answer *is* one of
        # them; the fitted rotation only differs from it by how crooked the
        # slide was. Snapping keeps the 3D model square, and snap_error_deg is
        # reported so a genuinely angled mounting shows up rather than being
        # quietly rounded away.
        self.dash.cal.mount = result.snapped
        self._mount_known = True
        self.dash.autosave_calibration("the board's axis mapping")
        self.dash.refresh_alignment_combo()

        forward, left, up = (self._axis_word(row) for row in result.snapped)
        lines = [
            f"Away from you is the board's {forward}, your left is {left}, "
            f"and up is {up}.",
            f"Mapping: {alignment_name(result.snapped)}",
        ]
        if len(result.residuals_deg) > 1:
            worst = result.worst_deg
            lines.append(f"The two slides agreed to within {worst:.0f}°.")
        if result.snap_error_deg > 30.0:
            lines.append(
                f"The slide was about {result.snap_error_deg:.0f}° off straight, "
                "which is still comfortably inside the 45° that would change "
                "the answer — but check the 3D view moves the way the board "
                "does before trusting it."
            )
        self.detail.setText("\n".join(lines))

        if last:
            self.feedback.setText("Done — press Next")
        else:
            self.feedback.setText(
                f"Got it ({move.distance * 100:.0f} cm) — press Next, or do the "
                "sideways slide to double-check"
            )
        self._set_feedback_colour(theme.STATUS["good"])

    @staticmethod
    def _axis_word(row: np.ndarray) -> str:
        """Name the board axis a row of the mount matrix picks out.

        Row 0 of ``mount`` dotted with a board vector gives its forward
        component, so the axis that row selects is the board axis that points
        forward -- which is the whole answer this step exists to produce.
        """
        index = int(np.argmax(np.abs(row)))
        return f"{'+' if row[index] > 0 else '-'}{'XYZ'[index]}"

    # ------------------------------------------------------------------
    def _do_verify(self, sample, still: bool, ready: bool) -> None:
        if self._substate == "done":
            return
        if not (still and ready):
            self.feedback.setText("Put the board down flat and let go…")
            self._set_feedback_colour(theme.INK_MUTED)
            return

        cal = self.dash.cal
        accel = cal.apply_accel(sample.accel)
        gyro = cal.apply_gyro(sample.gyro)
        mag = cal.apply_mag(sample.mag)

        gravity = float(np.linalg.norm(accel))
        rotation = float(np.linalg.norm(gyro))
        field = float(np.linalg.norm(mag))

        checks = [
            (
                "The board reports no rotation while it sits still",
                rotation < 0.5,
                f"{rotation:.3f} deg/s",
                "Redo step 1.",
            ),
            (
                "Gravity measures 1.00 g",
                abs(gravity - 1.0) < 0.02,
                f"{gravity:.3f} g",
                "Redo step 2, resting each side against something solid.",
            ),
            (
                "Earth's magnetic field is in the normal range",
                25.0 <= field <= 65.0,
                f"{field:.0f} uT",
                "Redo step 3, further from metal and electronics.",
            ),
            (
                "The board's axes have been named",
                self._mount_known,
                alignment_name(self.dash.cal.mount) if self._mount_known
                else "not established",
                "Redo step 4, shoving the board flat across the table without "
                "turning it.",
            ),
        ]
        self._verify = [(name, ok, value) for name, ok, value, _fix in checks]
        self._substate = "done"

        total = len(checks)
        passed = sum(1 for _n, ok, _v, _f in checks if ok)
        lines = []
        for name, ok, value, fix in checks:
            mark = "✓" if ok else "✗"
            lines.append(f"{mark}  {name}: {value}" + ("" if ok else f"   → {fix}"))
        self.detail.setText("\n".join(lines))

        if passed == total:
            self.feedback.setText(f"All {total} checks passed")
            self._set_feedback_colour(theme.STATUS["good"])
        elif passed >= total - 1:
            self.feedback.setText(f"{passed} of {total} passed — see below")
            self._set_feedback_colour(theme.STATUS["warning"])
        else:
            self.feedback.setText(f"{passed} of {total} passed — see below")
            self._set_feedback_colour(theme.STATUS["critical"])

        # Saving is allowed even on a partial pass: a calibration that fixes
        # two problems out of three still beats none, and the failure is named
        # so it can be redone.
        self.next_button.setEnabled(True)

    # ==================================================================
    # Files
    # ==================================================================
    def _save_to_file(self) -> None:
        """Write the calibration this screen has built to a JSON file.

        The board keeps its own copy in NVS, so this is not a backup of that
        so much as the only place two things live: the mounting orientation,
        which the board has no use for and is never pushed to it, and a
        calibration that outlives the board being reflashed.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, "Save calibration",
            str(Path.home() / "bbda_calibration.json"), "JSON (*.json)",
        )
        if not path:
            return
        try:
            self.dash.cal.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.feedback.setText("Saved to a file")
        self._set_feedback_colour(theme.STATUS["good"])
        self.detail.setText(f"Written to {path}")

    def _load_from_file(self) -> None:
        """Put a previously saved calibration back and go and check it.

        Deliberately not a shortcut to the end. A file is a claim about a
        board, and the only thing that can tell you it is still true of *this*
        board on *this* desk is the same physics the last step checks, so that
        is where loading leaves you rather than at 'all done'. Without a live
        connection there is nothing to check against, so it lands on the save
        step instead of stranding you on a screen waiting for readings that
        cannot arrive.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Load calibration", str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            self.dash.cal = Calibration.load(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not load", str(exc))
            return

        self.dash.refresh_calibration_labels()
        self.dash.refresh_alignment_combo()
        self.dash.autosave_calibration(f"calibration loaded from {path}")
        # The file carries a mounting, measured or not, so the check that asks
        # whether the axes have been named is satisfied by it.
        self._mount_known = True
        self._saved = False

        connected = self.dash.link.connected
        self._show_step(5 if connected else 6)
        self.detail.setText(
            f"Loaded from {path}.\n\n"
            + ("Now checking it against this board — the readings have to agree "
               "with it, and they will not if it came from a different board or "
               "the magnetic surroundings have changed."
               if connected else
               "Not connected, so there is nothing to check it against yet. "
               "Connect and press Back to run the checks.")
        )

    # ------------------------------------------------------------------
    def _save(self) -> None:
        if not self.dash.link.connected:
            self.feedback.setText("Not connected to the board")
            self._set_feedback_colour(theme.STATUS["critical"])
            return
        for command in self.dash.cal.to_device_commands():
            self.dash.link.send(command)
        self.dash.refresh_calibration_labels()
        self._saved = True
        self._show_step(7)
