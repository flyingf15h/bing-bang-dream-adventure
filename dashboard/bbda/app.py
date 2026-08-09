"""Main window.

Data arrives on the serial reader thread, is turned into records by
:mod:`bbda.link`, and lands here on the GUI thread. Every sample updates the
orientation filter and the ring buffers immediately; redrawing happens on a
30 Hz timer instead, so a 500 Hz stream costs no more paint work than a slow
one.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
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
    load_autosave,
    right_handed_alignments,
)
from .fusion import MadgwickAHRS, tilt_from_accel
from .link import (
    DEFAULT_UDP_PORT,
    Event,
    Sample,
    SerialLink,
    UdpLink,
    available_ports,
)
from .guide import GUIDE_HTML
from .motion import (
    FLICK_FRONT_CHOICES,
    PLANE_NAMES,
    DeadReckoning,
    Flick,
    FlickDetector,
    Flip,
    FlipDetector,
    KalmanDeadReckoning,
    QuickMove,
    QuickMoveDetector,
    SectorMap,
    flick_bearing_map,
    flick_frame,
    frame_sector_map,
)
from .view3d import MagScatterView, OrientationView, TrajectoryView
from .wizard import EasyModeWizard
from .widgets import FlowLayout, RingBuffer, StatTile, VectorReadout, make_plot

BUFFER_CAPACITY = 12000
WINDOW_CHOICES = {"5 s": 5.0, "10 s": 10.0, "30 s": 30.0, "60 s": 60.0}

# Roomy enough for the plots to be worth reading, clamped at startup to
# whatever the display can actually show.
PREFERRED_SIZE = (1520, 940)

# Ceiling for the two Motion-tab views. They sit on a scrolling page, where an
# unbounded view grows to its own generous size hint and buries the controls.
VIEW_MAX_HEIGHT = 380

ACCEL_ODR_CHOICES = ["12", "25", "50", "100", "200", "400", "800", "1600", "3200", "6400"]
ACCEL_FSR_CHOICES = ["2", "4", "8", "16"]
GYRO_FSR_CHOICES = ["15", "31", "62", "125", "250", "500", "1000", "2000"]

# How many directions the two gesture detectors quantise into. "Board axes"
# is the original behaviour: nearest of the six signed axes, in 3D. The rest
# divide one plane into that many equal sectors.
DIRECTION_CHOICES = [
    ("Board axes (6)", None),
    ("4 directions", 4),
    ("6 directions", 6),
    ("8 directions", 8),
    ("12 directions", 12),
]

# Six directions read as degrees from the board's front is the setting the
# game wants, so it is the one the dashboard starts in.
DEFAULT_FLICK_DIRECTIONS = 6

# What the sector count is measured against, once there is a sector count.
# ``None`` means the direction the flick went, in degrees clockwise from the
# board's front -- 0 up, 90 right -- which is the only one of these that names
# the gesture rather than the sensor reading. The rest divide up the raw
# rotation vector in one plane of board axes, which is what a bench check
# wants: the answer is then the axis the board turned about, unmassaged.
FLICK_FRAME_CHOICES = [("Degrees from the front", None)] + [
    (f"{name} plane, rotation axis", axes) for axes, name in PLANE_NAMES.items()
]

ESTIMATOR_CHOICES = ["Kalman + ZUPT", "Simple integrator"]

# How long a connected board may go without sending anything before the window
# says so. The stream runs at 100 Hz, so two seconds is two hundred missing
# records -- far outside anything a busy machine or a dropped datagram
# explains, and short enough to catch a pulled cable while the hand is still
# on it.
STREAM_STALL_S = 2.0

APEX_FEATURES = [
    ("tilt", "Tilt detection", "Fires when the board leans past 35 degrees"),
    ("ped", "Pedometer", "Step count, cadence and walk/run classification"),
    ("tap", "Tap detection", "Single and double tap, with axis and direction"),
    ("r2w", "Raise to wake", "Wake and sleep gestures"),
    ("freefall", "Free fall", "Reports the fall duration in milliseconds"),
    ("lowg", "Low-g", "Threshold crossing below 1 g"),
    ("highg", "High-g", "Threshold crossing above 1 g"),
]


class Dashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("bing-bang-dream-adventure  -  ICM-45605 + QMC6309")

        pg.setConfigOptions(antialias=True)

        # Both transports exist for the whole session and both are wired to
        # the same slots; only one is ever open, and `link` is whichever the
        # user selected. Building them up front means switching between them
        # is a pointer change rather than a rewiring exercise.
        self.serial_link = SerialLink()
        self.udp_link = UdpLink()
        self.link = self.serial_link

        self.fusion = MadgwickAHRS(beta=0.05)
        # Whatever was measured last time, without anyone having to remember
        # to save or load it. Falls back to the identity calibration on a
        # first run or an unreadable file.
        restored = load_autosave()
        self.cal = restored or Calibration()
        self._restored_calibration = restored is not None

        # Set up before anything can ask for a save. See autosave_calibration.
        self._autosave_reasons: set[str] = set()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(200)
        self._autosave_timer.timeout.connect(self._write_autosave)

        # Ring buffers: time plus three channels for each of the three sensors.
        self.buf_t = RingBuffer(BUFFER_CAPACITY)
        self.buf_accel = [RingBuffer(BUFFER_CAPACITY) for _ in range(3)]
        self.buf_gyro = [RingBuffer(BUFFER_CAPACITY) for _ in range(3)]
        self.buf_mag = [RingBuffer(BUFFER_CAPACITY) for _ in range(3)]

        self._t0: float | None = None
        self._last_t: float | None = None
        self._latest: Sample | None = None
        self._latest_cal: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

        self._rate_count = 0
        self._rate_mark = time.monotonic()
        self._sample_rate = 0.0

        # Link health. The wall clock, not the device timestamp: the question
        # is whether records are still arriving here, and a board that has
        # stopped sending has stopped advancing its own clock too.
        self._last_sample_wall = time.monotonic()
        self._link_alert: str | None = None
        self._closing_link = False

        # Both estimators are kept alive so switching between them does not
        # mean losing the comparison; only the selected one is fed.
        self.kalman_estimator = KalmanDeadReckoning()
        self.simple_estimator = DeadReckoning()
        self.dead_reckoning = self.kalman_estimator

        self.flick_detector = FlickDetector()
        self.flip_detector = FlipDetector()
        self.quick_move_detector = QuickMoveDetector(sector_map=frame_sector_map(4))
        self._last_flick: Flick | None = None
        self._last_flip: Flip | None = None
        self._last_move: QuickMove | None = None

        self._cal_mode: str | None = None
        self._gyro_collector = GyroBiasCollector()
        self._accel_collector = AccelSixPointCollector()
        self._mag_collector = MagCollector()
        self._mag_fit = None

        self._build_ui()
        self._connect_signals()
        self._size_to_screen()

        # Push the defaults through the same handlers the widgets use, so the
        # labels, hints and detector settings start out agreeing with the
        # controls rather than being duplicated in two places.
        self._on_estimator_changed(self.estimator_combo.currentIndex())
        self._on_flick_directions()
        self._on_move_directions()
        self._on_move_threshold(self.move_threshold.value())

        if self._restored_calibration:
            self._refresh_calibration_labels()
            self.refresh_alignment_combo()
            self.console.appendPlainText(
                f"> restored the calibration saved at {AUTOSAVE_PATH}"
            )

        self.render_timer = QTimer(self)
        self.render_timer.timeout.connect(self._render)
        self.render_timer.start(33)

        self.refresh_ports()

    def _size_to_screen(self) -> None:
        """Open at a comfortable size that still fits the display.

        A window larger than the screen is not merely ugly: the title bar ends
        up off the bottom on some window managers, so it cannot be dragged back
        into reach. Preferred size first, then clamped to what is actually
        available.
        """
        available = self.screen().availableGeometry()
        width = min(PREFERRED_SIZE[0], available.width())
        height = min(PREFERRED_SIZE[1], available.height())
        self.resize(width, height)
        self.move(
            available.x() + (available.width() - width) // 2,
            available.y() + max(0, (available.height() - height) // 2),
        )

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build_ui(self) -> None:
        self.setStyleSheet(theme.stylesheet())

        # Two screens: the normal dashboard, and easy mode which replaces it
        # outright rather than appearing as yet another panel to interpret.
        self.screens = QStackedWidget()
        self.setCentralWidget(self.screens)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(10)

        outer.addWidget(self._build_toolbar())

        # Hidden until something is wrong with the link. A stalled stream is
        # otherwise indistinguishable from a board sitting perfectly still,
        # which is the exact state half of these screens ask you to produce.
        self.alert_banner = QLabel()
        self.alert_banner.setWordWrap(True)
        self.alert_banner.setVisible(False)
        self.alert_banner.setStyleSheet(
            f"color: {theme.STATUS['critical']}; font-weight: 600;"
            f" border: 1px solid {theme.STATUS['critical']}; border-radius: 6px;"
            " padding: 7px 10px;"
        )
        outer.addWidget(self.alert_banner)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([620, 880])
        outer.addWidget(splitter, 1)

        self.screens.addWidget(central)
        self.wizard = EasyModeWizard(self)
        self.wizard.finished.connect(self._exit_easy_mode)
        self.screens.addWidget(self.wizard)

        quit_action = QAction(self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["USB serial", "WiFi (UDP)"])
        self.transport_combo.setToolTip(
            "Serial is lossless and needs no setup. UDP is untethered but "
            "datagrams can be dropped; the board must have WiFi credentials."
        )

        # One target editor per transport, swapped rather than shown together
        # so there is never an ambiguous "which of these two am I connecting
        # to" moment.
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(300)
        self.refresh_button = QPushButton("Refresh")

        serial_target = QWidget()
        serial_row = QHBoxLayout(serial_target)
        serial_row.setContentsMargins(0, 0, 0, 0)
        serial_row.setSpacing(8)
        serial_row.addWidget(QLabel("Port"))
        serial_row.addWidget(self.port_combo)
        serial_row.addWidget(self.refresh_button)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("192.168.1.50")
        self.host_input.setMinimumWidth(180)
        self.host_input.setToolTip(
            "The board prints its address in its banner and in reply to "
            "'wifi status'."
        )
        self.udp_port_input = QLineEdit(str(DEFAULT_UDP_PORT))
        self.udp_port_input.setFixedWidth(64)

        udp_target = QWidget()
        udp_row = QHBoxLayout(udp_target)
        udp_row.setContentsMargins(0, 0, 0, 0)
        udp_row.setSpacing(8)
        udp_row.addWidget(QLabel("Board address"))
        udp_row.addWidget(self.host_input)
        udp_row.addWidget(QLabel("port"))
        udp_row.addWidget(self.udp_port_input)

        self.target_stack = QStackedWidget()
        self.target_stack.addWidget(serial_target)
        self.target_stack.addWidget(udp_target)
        self.target_stack.setFixedHeight(serial_target.sizeHint().height())

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("primary")

        self.status_label = QLabel("Not connected")
        self.status_label.setObjectName("hint")

        self.rate_label = QLabel("--")
        self.rate_label.setObjectName("hint")

        row.addWidget(QLabel("Link"))
        row.addWidget(self.transport_combo)
        row.addWidget(self.target_stack)
        row.addWidget(self.connect_button)
        row.addSpacing(14)
        row.addWidget(self.status_label)
        row.addStretch(1)
        row.addWidget(self.rate_label)
        return bar

    # ------------------------------------------------------------------
    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        view_box = QGroupBox("Orientation")
        view_layout = QVBoxLayout(view_box)
        view_layout.setContentsMargins(8, 6, 8, 8)

        self.view3d = OrientationView()
        self.view3d.setMinimumHeight(380)
        view_layout.addWidget(self.view3d, 1)

        options = QHBoxLayout()
        self.chk_gravity = QCheckBox("Gravity vector")
        self.chk_gravity.setChecked(True)
        self.chk_mag = QCheckBox("Magnetic vector")
        self.chk_mag.setChecked(True)
        self.chk_trail = QCheckBox("Heading trail")
        self.chk_trail.setChecked(True)
        self.reset_view_button = QPushButton("Reset camera")
        options.addWidget(self.chk_gravity)
        options.addWidget(self.chk_mag)
        options.addWidget(self.chk_trail)
        options.addStretch(1)
        options.addWidget(self.reset_view_button)
        view_layout.addLayout(options)

        legend = QLabel(
            "Solid slab = board.  Coloured arms = board X / Y / Z.  "
            "Grey arm 'g' = gravity, amber arm 'B' = magnetic field.  "
            "With a good calibration both stay still while the board turns."
        )
        legend.setObjectName("hint")
        legend.setWordWrap(True)
        view_layout.addWidget(legend)

        layout.addWidget(view_box, 1)

        numbers = QGroupBox("Attitude")
        grid = QGridLayout(numbers)
        grid.setContentsMargins(10, 6, 10, 8)

        self.tile_roll = StatTile("Roll", "deg")
        self.tile_pitch = StatTile("Pitch", "deg")
        self.tile_yaw = StatTile("Yaw", "deg")
        self.tile_heading = StatTile("Heading", "deg from north")
        self.tile_quat = StatTile("Quaternion", "w x y z")
        self.tile_tilt = StatTile("Accel-only roll / pitch", "deg")

        grid.addWidget(self.tile_roll, 0, 0)
        grid.addWidget(self.tile_pitch, 0, 1)
        grid.addWidget(self.tile_yaw, 0, 2)
        grid.addWidget(self.tile_heading, 0, 3)
        grid.addWidget(self.tile_quat, 1, 0, 1, 2)
        grid.addWidget(self.tile_tilt, 1, 2, 1, 2)
        layout.addWidget(numbers)

        return panel

    # ------------------------------------------------------------------
    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """Let a page be taller than the window instead of enlarging it.

        Without this a tab's natural height becomes a hard floor on the whole
        window -- the Motion tab's explanatory text alone pushed the minimum
        past what a 1366x768 laptop can show, and Qt silently refuses any
        smaller resize, so the bottom of the window is unreachable.

        Only for pages that are genuinely a stack of content. A page whose job
        is to fill the viewport with plots must not be wrapped: pyqtgraph asks
        for 480 px per plot, so inside a scroll area three of them demand
        1600 px and scroll one-at-a-time instead of sharing the height.
        """
        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        return area

    def _build_tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "Live")
        self.tabs.addTab(self._scrollable(self._build_motion_tab()), "Motion")
        self.tabs.addTab(self._build_sensors_tab(), "Sensors")
        self.tabs.addTab(self._build_calibration_tab(), "Calibrate")
        self.tabs.addTab(self._build_events_tab(), "Events")
        self.tabs.addTab(self._build_console_tab(), "Console")
        return self.tabs

    def _build_live_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.readout_accel = VectorReadout("Accelerometer", "g", "{:+7.3f}")
        self.readout_gyro = VectorReadout("Gyroscope", "dps", "{:+7.2f}")
        self.readout_mag = VectorReadout("Magnetometer", "uT", "{:+7.2f}")
        # Wraps to two rows on a narrow window rather than clipping the last
        # sensor or forcing the whole tab to scroll sideways.
        header = FlowLayout(spacing=18)
        for readout in (self.readout_accel, self.readout_gyro, self.readout_mag):
            header.addWidget(readout)
        layout.addLayout(header)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet(f"color: {theme.BORDER_SOLID};")
        layout.addWidget(divider)

        strip = QHBoxLayout()
        self.tile_temp = StatTile("Die temperature", "C")
        self.tile_field = StatTile("Field strength", "uT")
        self.tile_steps = StatTile("Steps", "count")
        self.tile_activity = StatTile("Activity", "")
        strip.addWidget(self.tile_temp)
        strip.addWidget(self.tile_field)
        strip.addWidget(self.tile_steps)
        strip.addWidget(self.tile_activity)
        strip.addStretch(1)

        strip.addWidget(QLabel("Window"))
        self.window_combo = QComboBox()
        self.window_combo.addItems(WINDOW_CHOICES.keys())
        self.window_combo.setCurrentText("10 s")
        strip.addWidget(self.window_combo)

        self.chk_apply_cal = QCheckBox("Apply calibration")
        self.chk_apply_cal.setChecked(True)
        strip.addWidget(self.chk_apply_cal)
        layout.addLayout(strip)

        self.plot_accel, self.curves_accel = make_plot("Accelerometer", "g")
        self.plot_gyro, self.curves_gyro = make_plot("Gyroscope", "dps")
        self.plot_mag, self.curves_mag = make_plot("Magnetometer", "uT")
        layout.addWidget(self.plot_accel, 1)
        layout.addWidget(self.plot_gyro, 1)
        layout.addWidget(self.plot_mag, 1)

        return page

    # ------------------------------------------------------------------
    def _build_motion_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        warning = QLabel(
            "Position here is dead reckoning: acceleration integrated twice, with "
            "no external reference to correct it. Error grows with the SQUARE of "
            "time moving -- a residual bias of 10 mg is about 5 cm after 1 s and "
            "5 m after 10 s. Zero-velocity updates reset it every time the board "
            "is detected as still, so treat this as relative motion over the last "
            "few seconds, never as a position fix."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            f"color: {theme.STATUS['warning']}; border: 1px solid {theme.STATUS['warning']};"
            f" border-radius: 6px; padding: 7px;"
        )
        layout.addWidget(warning)

        views = QHBoxLayout()

        traj_box = QGroupBox("3D path")
        traj_layout = QVBoxLayout(traj_box)
        self.traj_view = TrajectoryView()
        # This page scrolls, so the views take a band rather than whatever
        # height they ask for -- left to themselves they fill the viewport and
        # push the controls that drive them off the bottom.
        self.traj_view.setMinimumHeight(260)
        self.traj_view.setMaximumHeight(VIEW_MAX_HEIGHT)
        traj_layout.addWidget(self.traj_view, 1)
        traj_hint = QLabel("Grid squares are 10 cm. Triad shows attitude at the tip.")
        traj_hint.setObjectName("hint")
        traj_layout.addWidget(traj_hint)
        views.addWidget(traj_box, 1)

        map_box = QGroupBox("2D map  (top-down, world X - Y plane)")
        map_layout = QVBoxLayout(map_box)
        self.map_plot = pg.PlotWidget()
        self.map_plot.setMinimumHeight(260)
        self.map_plot.setMaximumHeight(VIEW_MAX_HEIGHT)
        self.map_plot.setBackground(theme.SURFACE)
        self.map_plot.showGrid(x=True, y=True, alpha=0.2)
        self.map_plot.setAspectLocked(True)
        self.map_plot.setLabel("bottom", "east / north-ward X", units="m",
                               color=theme.INK_MUTED)
        self.map_plot.setLabel("left", "Y", units="m", color=theme.INK_MUTED)
        for side in ("left", "bottom"):
            axis = self.map_plot.getAxis(side)
            axis.setPen(pg.mkPen(theme.BASELINE, width=1))
            axis.setTextPen(pg.mkPen(theme.INK_MUTED))
        self.map_curve = self.map_plot.plot(
            pen=pg.mkPen(theme.AXIS_COLORS["x"], width=2), antialias=True, name="path"
        )
        self.map_start = self.map_plot.plot(
            [0], [0], pen=None, symbol="o", symbolSize=9,
            symbolBrush=pg.mkBrush(theme.INK_MUTED), name="start"
        )
        self.map_here = self.map_plot.plot(
            [0], [0], pen=None, symbol="o", symbolSize=12,
            symbolBrush=pg.mkBrush(theme.STATUS["good"]), name="now"
        )
        map_layout.addWidget(self.map_plot, 1)
        map_hint = QLabel("Grey dot = origin, green dot = current estimate.")
        map_hint.setObjectName("hint")
        map_layout.addWidget(map_hint)
        views.addWidget(map_box, 1)

        layout.addLayout(views, 1)

        tiles = QHBoxLayout()
        self.tile_pos = StatTile("Position X / Y / Z", "m")
        self.tile_vel = StatTile("Speed", "m/s")
        self.tile_dist = StatTile("Path length", "m")
        self.tile_still = StatTile("State", "")
        self.tile_drift = StatTile("Drift since last ZUPT", "m")
        for t in (self.tile_pos, self.tile_vel, self.tile_dist, self.tile_still,
                  self.tile_drift):
            tiles.addWidget(t)
        layout.addLayout(tiles)

        controls = QHBoxLayout()

        dr_box = QGroupBox("Dead reckoning")
        dr_grid = QGridLayout(dr_box)
        self.estimator_combo = QComboBox()
        self.estimator_combo.addItems(ESTIMATOR_CHOICES)
        self.chk_zupt = QCheckBox("Zero-velocity updates")
        self.chk_zupt.setChecked(True)
        self.reset_origin_button = QPushButton("Reset origin")
        self.reset_dr_button = QPushButton("Reset everything")
        self.damping_slider = QSlider(Qt.Horizontal)
        self.damping_slider.setRange(0, 300)   # damping = value / 100 per second
        self.damping_slider.setValue(80)
        self.damping_label = QLabel("0.80 /s")

        dr_grid.addWidget(QLabel("Estimator"), 0, 0)
        dr_grid.addWidget(self.estimator_combo, 0, 1, 1, 2)
        dr_grid.addWidget(self.chk_zupt, 1, 0, 1, 3)
        dr_grid.addWidget(QLabel("Velocity leak"), 2, 0)
        dr_grid.addWidget(self.damping_slider, 2, 1)
        dr_grid.addWidget(self.damping_label, 2, 2)
        dr_grid.addWidget(self.reset_origin_button, 3, 0)
        dr_grid.addWidget(self.reset_dr_button, 3, 1)

        self.estimator_hint = QLabel()
        self.estimator_hint.setObjectName("hint")
        self.estimator_hint.setWordWrap(True)
        dr_grid.addWidget(self.estimator_hint, 4, 0, 1, 3)
        controls.addWidget(dr_box, 1)

        flick_box = QGroupBox("Flick, flip and quick movement")
        flick_grid = QGridLayout(flick_box)

        self.flick_threshold = QSlider(Qt.Horizontal)
        self.flick_threshold.setRange(40, 1000)
        self.flick_threshold.setValue(150)
        self.flick_threshold_label = QLabel("150 dps")

        self.flick_dominance = QSlider(Qt.Horizontal)
        self.flick_dominance.setRange(30, 95)
        self.flick_dominance.setValue(80)
        self.flick_dominance_label = QLabel("0.80")

        self.flick_directions = QComboBox()
        for index, (label, count) in enumerate(DIRECTION_CHOICES):
            self.flick_directions.addItem(label)
            if count == DEFAULT_FLICK_DIRECTIONS:
                self.flick_directions.setCurrentIndex(index)
        self.flick_plane = QComboBox()
        for label, axes in FLICK_FRAME_CHOICES:
            self.flick_plane.addItem(label, axes)

        # Which axis points away from you. Everything about naming a flick as
        # a direction hangs off this: get it wrong and a roll about the real
        # front is what gets reported as up and down.
        self.flick_front = QComboBox()
        for axis in FLICK_FRONT_CHOICES:
            self.flick_front.addItem(f"Front is {axis}", axis)

        flick_grid.addWidget(QLabel("Trigger rate"), 0, 0)
        flick_grid.addWidget(self.flick_threshold, 0, 1)
        flick_grid.addWidget(self.flick_threshold_label, 0, 2)
        flick_grid.addWidget(QLabel("Axis dominance"), 1, 0)
        flick_grid.addWidget(self.flick_dominance, 1, 1)
        flick_grid.addWidget(self.flick_dominance_label, 1, 2)
        flick_grid.addWidget(QLabel("Flick directions"), 2, 0)
        flick_grid.addWidget(self.flick_directions, 2, 1)
        flick_grid.addWidget(self.flick_plane, 2, 2)
        flick_grid.addWidget(self.flick_front, 3, 1, 1, 2)

        self.move_directions = QComboBox()
        for label, count in DIRECTION_CHOICES:
            if count is not None:
                self.move_directions.addItem(label, count)
        self.move_directions.setCurrentIndex(0)
        self.move_threshold = QSlider(Qt.Horizontal)
        self.move_threshold.setRange(5, 100)     # m/s^2 = value / 10
        self.move_threshold.setValue(25)
        self.move_threshold_label = QLabel("2.5 m/s²")

        flick_grid.addWidget(QLabel("Move trigger"), 4, 0)
        flick_grid.addWidget(self.move_threshold, 4, 1)
        flick_grid.addWidget(self.move_threshold_label, 4, 2)
        flick_grid.addWidget(QLabel("Move directions"), 5, 0)
        flick_grid.addWidget(self.move_directions, 5, 1, 1, 2)

        self.tile_flick = StatTile("Last flick", "")
        self.tile_flip = StatTile("Board face up", "")
        self.tile_move = StatTile("Last movement", "")
        flick_grid.addWidget(self.tile_flick, 6, 0, 1, 2)
        flick_grid.addWidget(self.tile_flip, 7, 0, 1, 2)
        flick_grid.addWidget(self.tile_move, 8, 0, 1, 3)

        flick_hint = QLabel(
            "A flick is a short sharp rotation, a quick movement is a short "
            "sharp shove -- slide the board across the desk, or lift it. Both "
            "can name the direction either as the nearest board axis or as one "
            "of N equal sectors, for any N. More sectors means finer answers and "
            "more gestures refused for landing on a boundary, which is the honest "
            "trade: a direction that close to the line between two sectors was "
            "not really either of them.\n\n"
            "'Degrees from the front' names a flick by where it went, clockwise "
            "from straight up: 0° up, 90° right, 180° down, 270° left, so six "
            "directions come out as 0, 60, 120, 180, 240 and 300. Up and down "
            "are pitches, left and right are yaws, and a roll about the front is "
            "refused — it leaves the front pointing exactly where it was, so it "
            "is not a direction. Picking a plane instead names the axis the "
            "board turned about, which is 90° away from that and is for checking "
            "the sensor rather than playing.\n\n"
            "'Front is …' has to match the board. Run easy mode's orientation "
            "step and +X is correct, because that step rotates the board's axes "
            "into forward / left / up. Without it, the axes are whatever the "
            "silkscreen says — and naming the wrong one as the front is exactly "
            "what makes rolls come out as up and down. Flick the board upwards: "
            "if it reads 0° you have the right one.\n\n"
            "Movement directions are named forward / left / back / right once "
            "easy mode has worked out which way the board faces. A flip is a "
            "settled change of which face points up, so turning the board over "
            "slowly registers too."
        )
        flick_hint.setObjectName("hint")
        flick_hint.setWordWrap(True)
        flick_grid.addWidget(flick_hint, 9, 0, 1, 3)
        controls.addWidget(flick_box, 1)

        layout.addLayout(controls)
        return page

    # ------------------------------------------------------------------
    def _build_sensors_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        imu_box = QGroupBox("ICM-45605")
        imu_grid = QGridLayout(imu_box)
        imu_grid.setContentsMargins(10, 8, 10, 10)
        imu_grid.setHorizontalSpacing(10)

        self.accel_odr = QComboBox(); self.accel_odr.addItems(ACCEL_ODR_CHOICES)
        self.accel_odr.setCurrentText("200")
        self.accel_fsr = QComboBox(); self.accel_fsr.addItems(ACCEL_FSR_CHOICES)
        self.accel_fsr.setCurrentText("16")
        self.accel_apply = QPushButton("Apply")

        self.gyro_odr = QComboBox(); self.gyro_odr.addItems(ACCEL_ODR_CHOICES)
        self.gyro_odr.setCurrentText("200")
        self.gyro_fsr = QComboBox(); self.gyro_fsr.addItems(GYRO_FSR_CHOICES)
        self.gyro_fsr.setCurrentText("2000")
        self.gyro_apply = QPushButton("Apply")

        imu_grid.addWidget(QLabel("Accelerometer"), 0, 0)
        imu_grid.addWidget(QLabel("ODR (Hz)"), 0, 1)
        imu_grid.addWidget(self.accel_odr, 0, 2)
        imu_grid.addWidget(QLabel("Range (g)"), 0, 3)
        imu_grid.addWidget(self.accel_fsr, 0, 4)
        imu_grid.addWidget(self.accel_apply, 0, 5)

        imu_grid.addWidget(QLabel("Gyroscope"), 1, 0)
        imu_grid.addWidget(QLabel("ODR (Hz)"), 1, 1)
        imu_grid.addWidget(self.gyro_odr, 1, 2)
        imu_grid.addWidget(QLabel("Range (dps)"), 1, 3)
        imu_grid.addWidget(self.gyro_fsr, 1, 4)
        imu_grid.addWidget(self.gyro_apply, 1, 5)

        note = QLabel(
            "This part tops out at 16 g and 2000 dps. Gyro ranges 15 / 31 / 62 are "
            "the datasheet's 15.625 / 31.25 / 62.5 dps settings."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        imu_grid.addWidget(note, 2, 0, 1, 6)
        layout.addWidget(imu_box)

        apex_box = QGroupBox("APEX motion algorithms  (INT1 on GPIO17)")
        apex_grid = QGridLayout(apex_box)
        apex_grid.setContentsMargins(10, 8, 10, 10)
        self.apex_checks: dict[str, QCheckBox] = {}
        for index, (key, label, description) in enumerate(APEX_FEATURES):
            check = QCheckBox(label)
            check.setChecked(True)
            hint = QLabel(description)
            hint.setObjectName("hint")
            apex_grid.addWidget(check, index // 2, (index % 2) * 2)
            apex_grid.addWidget(hint, index // 2, (index % 2) * 2 + 1)
            self.apex_checks[key] = check

        run_row = QHBoxLayout()
        run_row.addWidget(QLabel("INT1 owner"))
        self.run_mode = QComboBox()
        self.run_mode.addItems(["stream", "fifo", "wom"])
        run_row.addWidget(self.run_mode)
        run_hint = QLabel(
            "stream = APEX events + register reads,  fifo = watermark-driven FIFO,  "
            "wom = wake-on-motion in low power. Only one can own the pin."
        )
        run_hint.setObjectName("hint")
        run_hint.setWordWrap(True)
        run_row.addWidget(run_hint, 1)
        apex_grid.addLayout(run_row, 4, 0, 1, 4)
        layout.addWidget(apex_box)

        mag_box = QGroupBox("QMC6309")
        mag_grid = QGridLayout(mag_box)
        mag_grid.setContentsMargins(10, 8, 10, 10)

        self.mag_mode = QComboBox(); self.mag_mode.addItems(["susp", "norm", "single", "cont"])
        self.mag_mode.setCurrentText("norm")
        self.mag_odr = QComboBox(); self.mag_odr.addItems(["1", "10", "50", "100", "200"])
        self.mag_odr.setCurrentText("200")
        self.mag_range = QComboBox(); self.mag_range.addItems(["8", "16", "32"])
        self.mag_range.setCurrentText("8")
        self.mag_osr1 = QComboBox(); self.mag_osr1.addItems(["1", "2", "4", "8"])
        self.mag_osr1.setCurrentText("8")
        self.mag_osr2 = QComboBox(); self.mag_osr2.addItems(["1", "2", "4", "8", "16"])
        self.mag_osr2.setCurrentText("8")
        self.mag_sr = QComboBox(); self.mag_sr.addItems(["on", "setonly", "off"])

        for column, (label, widget) in enumerate(
            [
                ("Mode", self.mag_mode),
                ("ODR (Hz)", self.mag_odr),
                ("Range (G)", self.mag_range),
                ("OSR1", self.mag_osr1),
                ("OSR2", self.mag_osr2),
                ("Set/reset", self.mag_sr),
            ]
        ):
            mag_grid.addWidget(QLabel(label), 0, column)
            mag_grid.addWidget(widget, 1, column)

        self.mag_apply = QPushButton("Apply")
        self.mag_selftest = QPushButton("Self-test")
        self.mag_reset = QPushButton("Soft reset")
        mag_grid.addWidget(self.mag_apply, 1, 6)
        mag_grid.addWidget(self.mag_selftest, 1, 7)
        mag_grid.addWidget(self.mag_reset, 1, 8)

        self.mag_selftest_result = QLabel(
            "Self-test drives a known field into all three bridges. "
            "Each axis must land between -50 and -1 LSB."
        )
        self.mag_selftest_result.setObjectName("hint")
        self.mag_selftest_result.setWordWrap(True)
        mag_grid.addWidget(self.mag_selftest_result, 2, 0, 1, 9)
        layout.addWidget(mag_box)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def _build_calibration_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(10)

        # -- easy mode ------------------------------------------------
        easy_box = QGroupBox("New to this?")
        easy_layout = QVBoxLayout(easy_box)
        easy_hint = QLabel(
            "Easy mode walks you through the whole thing on its own screen, one "
            "physical instruction at a time. It works out which side of the "
            "board is up, waits until you are holding it still, and checks its "
            "own result at the end. No knowledge of any of this required."
        )
        easy_hint.setObjectName("hint")
        easy_hint.setWordWrap(True)
        easy_layout.addWidget(easy_hint)
        self.easy_button = QPushButton("Easy mode  \u2192")
        self.easy_button.setObjectName("primary")
        self.easy_button.setMinimumHeight(34)
        easy_layout.addWidget(self.easy_button)
        left.addWidget(easy_box)

        # -- gyroscope ------------------------------------------------
        gyro_box = QGroupBox("1.  Gyroscope bias")
        gyro_layout = QVBoxLayout(gyro_box)
        gyro_hint = QLabel(
            "Put the board on a solid surface and leave it alone. The average "
            "reading over the capture is the bias."
        )
        gyro_hint.setObjectName("hint")
        gyro_hint.setWordWrap(True)
        gyro_layout.addWidget(gyro_hint)

        gyro_row = QHBoxLayout()
        self.gyro_start = QPushButton("Capture 500 samples")
        self.gyro_progress = QProgressBar()
        self.gyro_progress.setRange(0, 500)
        gyro_row.addWidget(self.gyro_start)
        gyro_row.addWidget(self.gyro_progress, 1)
        gyro_layout.addLayout(gyro_row)

        self.gyro_result = QLabel("Not measured")
        self.gyro_result.setObjectName("hint")
        self.gyro_result.setWordWrap(True)
        gyro_layout.addWidget(self.gyro_result)
        left.addWidget(gyro_box)

        # -- accelerometer --------------------------------------------
        accel_box = QGroupBox("2.  Accelerometer, six positions")
        accel_layout = QVBoxLayout(accel_box)
        accel_hint = QLabel(
            "Hold the board still in each of the six orientations and capture. "
            "Each axis needs a +1 g and a -1 g reading to solve for offset and gain."
        )
        accel_hint.setObjectName("hint")
        accel_hint.setWordWrap(True)
        accel_layout.addWidget(accel_hint)

        self.accel_buttons: dict[str, QPushButton] = {}
        self.accel_status: dict[str, QLabel] = {}
        grid = QGridLayout()
        for index, (name, _axis, _sign, description) in enumerate(SIX_POSITIONS):
            button = QPushButton(name)
            button.setToolTip(description)
            # Without a floor the six rows collapse to unreadable slivers when
            # the column is short.
            button.setMinimumHeight(26)
            status = QLabel("not captured")
            status.setObjectName("hint")
            grid.addWidget(button, index // 2, (index % 2) * 2)
            grid.addWidget(status, index // 2, (index % 2) * 2 + 1)
            self.accel_buttons[name] = button
            self.accel_status[name] = status
        accel_layout.addLayout(grid)

        self.accel_progress = QProgressBar()
        self.accel_progress.setRange(0, 100)
        accel_layout.addWidget(self.accel_progress)

        self.accel_result = QLabel("Not measured")
        self.accel_result.setObjectName("hint")
        self.accel_result.setWordWrap(True)
        accel_layout.addWidget(self.accel_result)
        left.addWidget(accel_box)

        # -- filter tuning --------------------------------------------
        filter_box = QGroupBox("4.  Filter tuning")
        filter_layout = QGridLayout(filter_box)

        self.beta_slider = QSlider(Qt.Horizontal)
        self.beta_slider.setRange(1, 500)      # beta = value / 1000
        self.beta_slider.setValue(50)
        self.beta_label = QLabel("0.050")

        self.zeta_slider = QSlider(Qt.Horizontal)
        self.zeta_slider.setRange(0, 200)      # zeta = value / 10000
        self.zeta_slider.setValue(0)
        self.zeta_label = QLabel("0.0000")

        filter_layout.addWidget(QLabel("Beta  (gain)"), 0, 0)
        filter_layout.addWidget(self.beta_slider, 0, 1)
        filter_layout.addWidget(self.beta_label, 0, 2)
        filter_layout.addWidget(QLabel("Zeta  (bias tracking)"), 1, 0)
        filter_layout.addWidget(self.zeta_slider, 1, 1)
        filter_layout.addWidget(self.zeta_label, 1, 2)

        filter_hint = QLabel(
            "Low beta is smooth but slow to correct gyro drift; high beta follows "
            "the accelerometer and magnetometer closely and picks up their noise. "
            "Leave zeta at zero once the gyroscope bias is calibrated."
        )
        filter_hint.setObjectName("hint")
        filter_hint.setWordWrap(True)
        filter_layout.addWidget(filter_hint, 2, 0, 1, 3)

        self.reset_fusion = QPushButton("Reset orientation")
        filter_layout.addWidget(self.reset_fusion, 3, 0)
        left.addWidget(filter_box)

        # -- axis alignment -------------------------------------------
        align_box = QGroupBox("5.  Axis alignment  (make the model match the board)")
        align_layout = QVBoxLayout(align_box)

        align_hint = QLabel(
            "The 3D view draws the board's own X / Y / Z. If the model turns the "
            "wrong way, or the wrong way round, the sensor axes are not oriented "
            "the way the view assumes -- pick the mapping that makes them agree. "
            "Only the 24 mappings that are real rotations are offered; the other "
            "24 are mirrors, which would silently reverse every gyroscope reading."
        )
        align_hint.setObjectName("hint")
        align_hint.setWordWrap(True)
        align_layout.addWidget(align_hint)

        self.alignment_combo = QComboBox()
        for name, _matrix in right_handed_alignments():
            self.alignment_combo.addItem(name)
        align_layout.addWidget(self.alignment_combo)

        self.alignment_check = QLabel("")
        self.alignment_check.setObjectName("hint")
        self.alignment_check.setWordWrap(True)
        align_layout.addWidget(self.alignment_check)

        guide_button = QPushButton("Show the step-by-step guide")
        align_layout.addWidget(guide_button)
        self.guide_button = guide_button

        left.addWidget(align_box)
        left.addStretch(1)

        # -- persistence ----------------------------------------------
        persist = QGroupBox("Store")
        persist_layout = QGridLayout(persist)
        self.push_button = QPushButton("Push to board")
        self.push_button.setObjectName("primary")
        self.pull_button = QPushButton("Read from board")
        self.save_button = QPushButton("Save JSON")
        self.load_button = QPushButton("Load JSON")
        self.clear_button = QPushButton("Reset to identity")
        persist_layout.addWidget(self.push_button, 0, 0)
        persist_layout.addWidget(self.pull_button, 0, 1)
        persist_layout.addWidget(self.save_button, 1, 0)
        persist_layout.addWidget(self.load_button, 1, 1)
        persist_layout.addWidget(self.clear_button, 2, 0, 1, 2)
        persist_hint = QLabel(
            "Push writes the calibration into the board's NVS so its own "
            "pretty-printed output is corrected too.\n\n"
            f"A working copy is kept at {AUTOSAVE_PATH} without being asked: "
            "it is rewritten whenever anything here measures something, and "
            "read back at startup. Save and Load are for named copies "
            "elsewhere."
        )
        persist_hint.setObjectName("hint")
        persist_hint.setWordWrap(True)
        persist_layout.addWidget(persist_hint, 3, 0, 1, 2)
        left.addWidget(persist)

        left_widget = QWidget()
        left_widget.setLayout(left)

        # Four stacked groups do not fit a laptop-height window; scrolling
        # keeps every control at its natural size instead of compressing them.
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(430)
        layout.addWidget(left_scroll, 4)

        # -- magnetometer ---------------------------------------------
        mag_box = QGroupBox("3.  Magnetometer, hard and soft iron")
        mag_layout = QVBoxLayout(mag_box)
        mag_hint = QLabel(
            "Rotate the board slowly through every orientation you can reach -- "
            "figure-of-eight motions work well. An ellipsoid is fitted to the "
            "cloud: its centre is the hard-iron offset, its shape is the soft-iron "
            "distortion."
        )
        mag_hint.setObjectName("hint")
        mag_hint.setWordWrap(True)
        mag_layout.addWidget(mag_hint)

        mag_row = QHBoxLayout()
        self.mag_collect = QPushButton("Start collecting")
        self.mag_fit_button = QPushButton("Fit ellipsoid")
        self.mag_clear = QPushButton("Clear")
        mag_row.addWidget(self.mag_collect)
        mag_row.addWidget(self.mag_fit_button)
        mag_row.addWidget(self.mag_clear)
        mag_row.addStretch(1)
        mag_layout.addLayout(mag_row)

        coverage_row = QHBoxLayout()
        coverage_row.addWidget(QLabel("Coverage"))
        self.mag_coverage = QProgressBar()
        self.mag_coverage.setRange(0, 100)
        coverage_row.addWidget(self.mag_coverage, 1)
        self.mag_count = QLabel("0 points")
        self.mag_count.setObjectName("hint")
        self.mag_count.setMinimumWidth(80)
        coverage_row.addWidget(self.mag_count)
        mag_layout.addLayout(coverage_row)

        self.mag_scatter = MagScatterView()
        self.mag_scatter.setMinimumHeight(320)
        mag_layout.addWidget(self.mag_scatter, 1)

        scatter_legend = QLabel(
            "Orange cloud = raw samples.  Green cloud = after correction.  "
            "White dot = origin. A good fit turns an off-centre egg into a "
            "sphere around the dot."
        )
        scatter_legend.setObjectName("hint")
        scatter_legend.setWordWrap(True)
        mag_layout.addWidget(scatter_legend)

        self.mag_result = QLabel("Not measured")
        self.mag_result.setObjectName("hint")
        self.mag_result.setWordWrap(True)
        # The fit report runs to three lines; reserve the space so the scatter
        # view above cannot squeeze it off the bottom of the panel.
        self.mag_result.setMinimumHeight(52)
        mag_layout.addWidget(self.mag_result)

        layout.addWidget(mag_box, 5)
        return page

    # ------------------------------------------------------------------
    def _build_events_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)

        hint = QLabel(
            "Events raised by the ICM-45605's on-chip APEX engine, newest first. "
            "The host does no detection of its own -- these come off the interrupt pin."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.event_table = QTableWidget(0, 3)
        self.event_table.setHorizontalHeaderLabels(["Time (s)", "Event", "Detail"])
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.event_table.setColumnWidth(0, 100)
        self.event_table.setColumnWidth(1, 130)
        self.event_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.event_table, 1)

        row = QHBoxLayout()
        self.clear_events = QPushButton("Clear")
        row.addStretch(1)
        row.addWidget(self.clear_events)
        layout.addLayout(row)
        return page

    def _build_console_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(3000)
        layout.addWidget(self.console, 1)

        row = QHBoxLayout()
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText(
            "Type a firmware command and press Enter  -  try 'help'"
        )
        self.chk_show_data = QCheckBox("Show data records")
        row.addWidget(self.command_input, 1)
        row.addWidget(self.chk_show_data)
        layout.addLayout(row)
        return page

    # ==================================================================
    # Signals
    # ==================================================================
    def _connect_signals(self) -> None:
        for link in (self.serial_link, self.udp_link):
            link.sample.connect(self._on_sample)
            link.event.connect(self._on_event)
            link.info.connect(self._on_info)
            link.line.connect(self._on_line)
            link.status.connect(self._on_status)

        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self._toggle_connection)
        self.transport_combo.currentIndexChanged.connect(self._on_transport_changed)
        self.host_input.returnPressed.connect(self._toggle_connection)

        self.chk_gravity.toggled.connect(self.view3d.set_show_gravity)
        self.chk_mag.toggled.connect(self.view3d.set_show_mag)
        self.chk_trail.toggled.connect(self.view3d.set_show_trail)
        self.reset_view_button.clicked.connect(self.view3d.reset_view)

        self.accel_apply.clicked.connect(
            lambda: self.link.send(
                f"accel {self.accel_odr.currentText()} {self.accel_fsr.currentText()}"
            )
        )
        self.gyro_apply.clicked.connect(
            lambda: self.link.send(
                f"gyro {self.gyro_odr.currentText()} {self.gyro_fsr.currentText()}"
            )
        )
        for key, check in self.apex_checks.items():
            check.toggled.connect(
                lambda on, k=key: self.link.send(f"apex {k} {'on' if on else 'off'}")
            )
        self.run_mode.currentTextChanged.connect(lambda t: self.link.send(f"run {t}"))

        self.mag_apply.clicked.connect(self._apply_mag_config)
        self.mag_selftest.clicked.connect(lambda: self.link.send("mag selftest"))
        self.mag_reset.clicked.connect(lambda: self.link.send("mag reset"))

        self.gyro_start.clicked.connect(self._start_gyro_capture)
        for name, button in self.accel_buttons.items():
            button.clicked.connect(lambda _checked=False, n=name: self._start_accel_capture(n))
        self.mag_collect.clicked.connect(self._toggle_mag_collection)
        self.mag_fit_button.clicked.connect(self._fit_mag)
        self.mag_clear.clicked.connect(self._clear_mag)

        self.beta_slider.valueChanged.connect(self._on_beta_changed)
        self.zeta_slider.valueChanged.connect(self._on_zeta_changed)
        self.reset_fusion.clicked.connect(self.fusion.reset)

        self.push_button.clicked.connect(self._push_calibration)
        self.pull_button.clicked.connect(lambda: self.link.send("cal show"))
        self.save_button.clicked.connect(self._save_calibration)
        self.load_button.clicked.connect(self._load_calibration)
        self.clear_button.clicked.connect(self._reset_calibration)

        self.chk_zupt.toggled.connect(self._on_zupt_toggled)
        self.easy_button.clicked.connect(self._enter_easy_mode)
        self.alignment_combo.currentIndexChanged.connect(self._on_alignment_changed)
        self.guide_button.clicked.connect(self._show_guide)

        self.damping_slider.valueChanged.connect(self._on_damping_changed)
        self.reset_origin_button.clicked.connect(self._reset_origin)
        self.reset_dr_button.clicked.connect(self._reset_dead_reckoning)
        self.flick_threshold.valueChanged.connect(self._on_flick_threshold)
        self.flick_dominance.valueChanged.connect(self._on_flick_dominance)
        self.estimator_combo.currentIndexChanged.connect(self._on_estimator_changed)
        self.flick_directions.currentIndexChanged.connect(self._on_flick_directions)
        self.flick_plane.currentIndexChanged.connect(self._on_flick_directions)
        self.flick_front.currentIndexChanged.connect(self._on_flick_directions)
        self.move_directions.currentIndexChanged.connect(self._on_move_directions)
        self.move_threshold.valueChanged.connect(self._on_move_threshold)

        self.clear_events.clicked.connect(lambda: self.event_table.setRowCount(0))
        self.command_input.returnPressed.connect(self._send_command)

    # ==================================================================
    # Connection
    # ==================================================================
    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for device, label in available_ports():
            self.port_combo.addItem(label, device)
        if current is not None:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("No serial ports found", None)

    def _on_transport_changed(self, index: int) -> None:
        if self.link.connected:
            self.link.disconnect()
        self.link = self.serial_link if index == 0 else self.udp_link
        self.target_stack.setCurrentIndex(index)
        if index == 0:
            self.refresh_ports()

    def _toggle_connection(self) -> None:
        if self.link.connected:
            # Flagged so the status that comes back is understood as this
            # button rather than as the board vanishing.
            self._closing_link = True
            try:
                self.link.disconnect()
            finally:
                self._closing_link = False
            return

        if self.link is self.serial_link:
            target = self.port_combo.currentData()
            if not target:
                QMessageBox.warning(self, "No port", "Select a serial port first.")
                return
        else:
            host = self.host_input.text().strip()
            if not host:
                QMessageBox.warning(
                    self,
                    "No address",
                    "Type the board's IP address.\n\n"
                    "The board prints it in its startup banner and in reply to "
                    "the 'wifi status' command over serial.",
                )
                return
            port = self.udp_port_input.text().strip() or str(DEFAULT_UDP_PORT)
            target = f"{host}:{port}"

        self._reset_buffers()
        self.link.connect(target)

    def _on_status(self, message: str, connected: bool) -> None:
        self.status_label.setText(message)
        self.connect_button.setText("Disconnect" if connected else "Connect")
        color = theme.STATUS["good"] if connected else theme.INK_MUTED
        self.status_label.setStyleSheet(f"color: {color};")

        if connected:
            self._last_sample_wall = time.monotonic()
            self._set_link_alert(None)
        elif not self._closing_link:
            # Nobody pressed Disconnect, so the board went away by itself.
            self._set_link_alert(
                f"The board disconnected — {message}. Plug it back in and press "
                "Connect. Anything measured up to now is kept, and the "
                "calibration on disk is already up to date."
            )
        else:
            self._set_link_alert(None)

    # ------------------------------------------------------------------
    def _set_link_alert(self, message: str | None) -> None:
        """Show or clear the link warning, on whichever screen is in front."""
        if message == self._link_alert:
            return
        self._link_alert = message
        self.alert_banner.setText(message or "")
        self.alert_banner.setVisible(message is not None)
        # Easy mode replaces the whole window, banner included, so it has to be
        # told separately -- and it is the screen where a silent stall does the
        # most damage, since every step there is a wait for readings.
        self.wizard.set_link_alert(message)
        if message:
            self.console.appendPlainText(f"! {message}")

    def _check_link_health(self) -> None:
        """Notice a board that has stopped sending, however it stopped.

        A dropped USB link usually raises in the reader thread and tears the
        port down, which the status path reports. This covers what that misses:
        a USB device that goes quiet without erroring, a board that has crashed
        or been reset while still enumerated, and WiFi, where there is no
        connection to lose in the first place -- a UDP link's idea of "still
        connected" is only ever "the socket is still open".
        """
        if not self.link.connected:
            return
        gap = time.monotonic() - self._last_sample_wall
        if gap < STREAM_STALL_S:
            if self._link_alert:
                self._set_link_alert(None)
            return
        if self._link_alert:
            return      # already said so; do not re-word it every frame

        detail = (
            "check the cable" if self.link is self.serial_link
            else "check the board's power and WiFi"
        )
        if self.link is self.serial_link:
            target = self.port_combo.currentData()
            if target and target not in {device for device, _label in available_ports()}:
                detail = f"{target} is no longer there, so the cable is out"
        self._set_link_alert(
            f"No data from the board for {gap:.0f} s — {detail}. Readings on "
            "screen are frozen, not still."
        )

    def _send_command(self) -> None:
        text = self.command_input.text().strip()
        if not text:
            return
        self.console.appendPlainText(f"> {text}")
        self.link.send(text)
        self.command_input.clear()

    # ==================================================================
    # Incoming data
    # ==================================================================
    def _reset_buffers(self) -> None:
        self.buf_t.clear()
        for buffers in (self.buf_accel, self.buf_gyro, self.buf_mag):
            for buffer in buffers:
                buffer.clear()
        self._t0 = None
        self._last_t = None
        self.fusion.reset()
        self.kalman_estimator.reset()
        self.simple_estimator.reset()
        self.flick_detector.reset()
        self.flip_detector.reset()
        self.quick_move_detector.reset()

    def _on_sample(self, sample: Sample) -> None:
        self._last_sample_wall = time.monotonic()
        if self._t0 is None:
            self._t0 = sample.t
        elapsed = sample.t - self._t0

        if self.chk_apply_cal.isChecked():
            accel = self.cal.apply_accel(sample.accel)
            gyro = self.cal.apply_gyro(sample.gyro)
            mag = self.cal.apply_mag(sample.mag)
        else:
            accel, gyro, mag = sample.accel, sample.gyro, sample.mag

        self._latest = sample
        self._latest_cal = (accel, gyro, mag)

        dt = 0.0 if self._last_t is None else sample.t - self._last_t
        self._last_t = sample.t
        if dt > 0:
            self.fusion.update(gyro, accel, mag if sample.mag_fresh else None, dt)

        self.buf_t.append(elapsed)
        for index in range(3):
            self.buf_accel[index].append(accel[index])
            self.buf_gyro[index].append(gyro[index])
            self.buf_mag[index].append(mag[index])

        # Translation and gestures both run on the calibrated, mount-corrected
        # values, and both need dt, so they sit alongside the filter update.
        if dt > 0:
            state = self.dead_reckoning.update(
                self.fusion.rotation_matrix(), accel, gyro, dt
            )
            # The quick-move detector reuses the estimator's world-frame linear
            # acceleration rather than computing its own: that vector already
            # has gravity removed with the current attitude and the learned
            # accelerometer bias taken out, which is most of the work.
            move = self.quick_move_detector.update(sample.t, state.linear_accel, dt)
            if move is not None:
                self._on_quick_move(move)

        flick = self.flick_detector.update(sample.t, gyro, accel)
        if flick is not None:
            self._on_flick(flick)
        flip = self.flip_detector.update(sample.t, accel, gyro)
        if flip is not None:
            self._on_flip(flip)

        # Easy mode gets the RAW sample: it runs its own collectors, and
        # feeding it corrected values would fold the existing calibration into
        # the new one.
        if self.screens.currentIndex() == 1:
            self.wizard.on_sample(sample)

        self._feed_collectors(sample)

        self._rate_count += 1
        now = time.monotonic()
        if now - self._rate_mark >= 1.0:
            self._sample_rate = self._rate_count / (now - self._rate_mark)
            self._rate_count = 0
            self._rate_mark = now

    def _feed_collectors(self, sample: Sample) -> None:
        """Route the raw sample to whichever calibration capture is running.

        Collectors always take the *raw* reading -- calibrating on top of an
        already-applied correction would fold the old numbers into the new ones.
        """
        if self._cal_mode == "gyro":
            self._gyro_collector.add(sample.gyro)
            if self._gyro_collector.done:
                self._finish_gyro_capture()
        elif self._cal_mode == "accel":
            self._accel_collector.add(sample.accel)
            if not self._accel_collector.capturing:
                self._finish_accel_capture()
        elif self._cal_mode == "mag" and sample.mag_fresh:
            self._mag_collector.add(sample.mag)

    def _add_event_row(self, t: float, name: str, detail: str,
                       color: str | None = None) -> None:
        """Prepend a row to the event log. Shared by device and host events."""
        self.event_table.insertRow(0)
        self.event_table.setItem(0, 0, QTableWidgetItem(f"{t:.3f}"))
        name_item = QTableWidgetItem(name)
        # The label always names the event; colour is only a secondary cue.
        if color:
            name_item.setForeground(pg.mkColor(color))
        self.event_table.setItem(0, 1, name_item)
        self.event_table.setItem(0, 2, QTableWidgetItem(detail))
        if self.event_table.rowCount() > 500:
            self.event_table.removeRow(self.event_table.rowCount() - 1)

    def _on_event(self, event: Event) -> None:
        color = None
        if event.name in ("freefall", "highg", "lowg"):
            color = theme.STATUS["serious"]
        elif event.name == "wom":
            color = theme.STATUS["warning"]
        self._add_event_row(event.t, event.name, event.detail, color)

        if event.name == "pedometer":
            if "steps" in event.fields:
                self.tile_steps.set_value(event.fields["steps"])
            if "activity" in event.fields:
                self.tile_activity.set_value(event.fields["activity"])

    def _on_info(self, key: str, value: str) -> None:
        """Absorb `cal show` output coming back from the board."""
        numbers = []
        for token in value.replace(",", " ").split():
            try:
                numbers.append(float(token))
            except ValueError:
                continue

        try:
            if key == "cal.gyro_bias" and len(numbers) >= 3:
                self.cal.gyro_bias = np.array(numbers[:3])
            elif key == "cal.accel_bias" and len(numbers) >= 3:
                self.cal.accel_bias = np.array(numbers[:3])
            elif key == "cal.accel_scale" and len(numbers) >= 3:
                self.cal.accel_scale = np.array(numbers[:3])
            elif key == "cal.mag_bias" and len(numbers) >= 3:
                self.cal.mag_bias = np.array(numbers[:3])
            elif key == "cal.mag_soft" and len(numbers) >= 9:
                self.cal.mag_soft = np.array(numbers[:9]).reshape(3, 3)
            else:
                return
        except ValueError:
            return
        self._refresh_calibration_labels()
        self.autosave_calibration("values read from the board")

    def _on_line(self, text: str) -> None:
        if text.startswith("D,") and not self.chk_show_data.isChecked():
            return
        self.console.appendPlainText(text)
        if text.startswith("OK mag selftest") or text.startswith("ERR mag selftest"):
            passed = "PASS" in text
            self.mag_selftest_result.setText(text)
            self.mag_selftest_result.setStyleSheet(
                f"color: {theme.STATUS['good'] if passed else theme.STATUS['critical']};"
            )

    # ==================================================================
    # Rendering
    # ==================================================================
    def _render(self) -> None:
        # Before the early return below: a stalled stream is exactly the case
        # where there is nothing new to draw, and the one that most needs
        # saying.
        self._check_link_health()

        if len(self.buf_t) == 0:
            return

        times = self.buf_t.values()
        window = WINDOW_CHOICES[self.window_combo.currentText()]
        latest = times[-1]
        mask = times >= latest - window

        for buffers, curves in (
            (self.buf_accel, self.curves_accel),
            (self.buf_gyro, self.curves_gyro),
            (self.buf_mag, self.curves_mag),
        ):
            for index, axis in enumerate(("x", "y", "z")):
                curves[axis].setData(times[mask], buffers[index].values()[mask])

        # Until a full window has accumulated, clamp to the first sample so the
        # traces fill the axes instead of hugging the right edge.
        left = max(float(times[0]), latest - window)
        if latest - left < 1e-3:
            left = latest - window
        for plot in (self.plot_accel, self.plot_gyro, self.plot_mag):
            plot.setXRange(left, latest, padding=0)

        if self._latest is not None and self._latest_cal is not None:
            accel, gyro, mag = self._latest_cal
            self.readout_accel.set_vector(accel)
            self.readout_gyro.set_vector(gyro)
            self.readout_mag.set_vector(mag)
            self.tile_temp.set_value(f"{self._latest.temp:.2f}")
            self.tile_field.set_value(f"{np.linalg.norm(mag):.1f}")

            rotation = self.fusion.rotation_matrix()
            self.view3d.update_orientation(rotation, accel, mag)

            roll, pitch, yaw = self.fusion.euler_degrees()
            self.tile_roll.set_value(f"{roll:+.2f}")
            self.tile_pitch.set_value(f"{pitch:+.2f}")
            self.tile_yaw.set_value(f"{yaw:+.2f}")
            self.tile_heading.set_value(f"{self.fusion.heading_degrees():6.1f}")

            q = self.fusion.orientation_quaternion()
            self.tile_quat.set_value(
                f"{q[0]:+.3f} {q[1]:+.3f} {q[2]:+.3f} {q[3]:+.3f}"
            )

            tilt_roll, tilt_pitch = tilt_from_accel(accel)
            self.tile_tilt.set_value(f"{tilt_roll:+.1f} / {tilt_pitch:+.1f}")

        self._render_alignment_check()
        self._render_motion()

        self.rate_label.setText(f"{self._sample_rate:5.1f} samples/s")

        if self._cal_mode == "gyro":
            self.gyro_progress.setValue(self._gyro_collector.count)
        elif self._cal_mode == "accel":
            self.accel_progress.setValue(int(self._accel_collector.progress * 100))
        elif self._cal_mode == "mag":
            self.mag_count.setText(f"{self._mag_collector.count} points")
            self.mag_coverage.setValue(int(self._mag_collector.coverage() * 100))
            if self._mag_collector.count % 10 == 0:
                self.mag_scatter.set_points(self._mag_collector.points)

    # ==================================================================
    # Axis alignment
    # ==================================================================
    def _on_alignment_changed(self, index: int) -> None:
        options = right_handed_alignments()
        if 0 <= index < len(options):
            self.cal.mount = options[index][1]
            self.fusion.reset()
            self.dead_reckoning.reset()
            self.autosave_calibration("axis mapping")

    def refresh_alignment_combo(self) -> None:
        """Point the combo at whatever ``cal.mount`` currently holds.

        Called after easy mode works the mounting out by watching the board
        move, so the expert panel agrees with what easy mode decided instead
        of silently contradicting it.
        """
        for index, (_name, matrix) in enumerate(right_handed_alignments()):
            if np.allclose(matrix, self.cal.mount, atol=1e-6):
                # Setting the index would re-enter _on_alignment_changed and
                # reset the filter for no reason -- the mount is already what
                # it is about to be set to.
                blocked = self.alignment_combo.blockSignals(True)
                self.alignment_combo.setCurrentIndex(index)
                self.alignment_combo.blockSignals(blocked)
                return

    def _render_alignment_check(self) -> None:
        """Live 'what is up right now' readout, so the guide can be followed.

        Reports the physical face the accelerometer says is pointing at the
        ceiling. Following the guide is a matter of checking that this agrees
        with reality, which needs no test equipment.
        """
        if self._latest_cal is None:
            return
        accel = self._latest_cal[0]
        found = FlipDetector.face_of(accel)
        if found is None:
            self.alignment_check.setText(
                "Hold the board still and level to read its orientation."
            )
            self.alignment_check.setStyleSheet("")
            return
        face, _index, _sign = found
        roll, pitch = tilt_from_accel(accel)
        self.alignment_check.setText(
            f"Right now: board {face} points up.  Roll {roll:+.0f} deg, "
            f"pitch {pitch:+.0f} deg.  Mapping: {alignment_name(self.cal.mount)}"
        )
        self.alignment_check.setStyleSheet(f"color: {theme.INK_SECONDARY};")

    def _enter_easy_mode(self) -> None:
        if not self.link.connected:
            QMessageBox.information(
                self,
                "Connect first",
                "Connect to the board before calibrating.\n\n"
                "Pick the port at the top of the window and press Connect.",
            )
            return
        # Any half-finished expert-panel capture would otherwise keep eating
        # samples behind the wizard's back.
        self._cal_mode = None
        self.wizard.restart()
        self.screens.setCurrentIndex(1)

    def _exit_easy_mode(self) -> None:
        self.screens.setCurrentIndex(0)
        self.tabs.setCurrentIndex(3)
        self._refresh_calibration_labels()
        self.refresh_alignment_combo()

    def refresh_calibration_labels(self) -> None:
        """Public alias used by the easy-mode screen after it saves."""
        self._refresh_calibration_labels()

    def _show_guide(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Calibration and axis alignment guide")
        box.setTextFormat(Qt.RichText)
        box.setText(GUIDE_HTML)
        box.setStandardButtons(QMessageBox.Close)
        box.exec()

    # ==================================================================
    # Motion
    # ==================================================================
    def _render_motion(self) -> None:
        state = self.dead_reckoning.state
        path = self.dead_reckoning.path

        self.traj_view.update_path(path, state.position, self.fusion.rotation_matrix())
        if len(path) >= 1:
            self.map_curve.setData(path[:, 0], path[:, 1])
            self.map_here.setData([state.position[0]], [state.position[1]])

        p = state.position
        self.tile_pos.set_value(f"{p[0]:+.3f}  {p[1]:+.3f}  {p[2]:+.3f}")
        self.tile_vel.set_value(f"{state.speed:.3f}")
        self.tile_dist.set_value(f"{state.distance:.3f}")
        self.tile_still.set_value(
            "stationary" if state.stationary else "moving",
            theme.STATUS["good"] if state.stationary else theme.STATUS["warning"],
        )
        # The Kalman filter carries a real uncertainty in its covariance, so
        # there is no need to fall back on the rule-of-thumb drift estimate.
        if self.dead_reckoning is self.kalman_estimator:
            drift = state.position_sigma
            text = f"{drift:.3f}"
        else:
            drift = state.drift_estimate_m
            text = f"~{drift:.2f}"
        self.tile_drift.set_value(
            text,
            theme.STATUS["good"] if drift < 0.1 else
            theme.STATUS["warning"] if drift < 1.0 else theme.STATUS["critical"],
        )

        face = self.flip_detector.current_face
        self.tile_flip.set_value(face if face else "--")

    def _on_flick(self, flick: Flick) -> None:
        self._last_flick = flick
        self.tile_flick.set_value(
            f"{flick.label}  {flick.peak_dps:.0f} dps",
            theme.AXIS_COLORS[flick.axis],
        )
        if flick.sector is not None:
            where = (
                f"sector={flick.sector.label} "
                f"({flick.sector.index + 1}/{flick.sector.count}) "
                f"angle={flick.sector.angle_deg:.0f}deg "
                f"margin={flick.sector.margin:.2f} in-plane={flick.dominance:.2f}"
            )
        else:
            where = (
                f"axis={flick.axis.upper()} "
                f"direction={'+' if flick.direction > 0 else '-'} "
                f"dominance={flick.dominance:.2f}"
            )
        self._add_event_row(
            flick.t,
            "flick",
            f"{where} peak={flick.peak_dps:.0f}dps "
            f"duration={flick.duration_ms:.0f}ms",
            theme.AXIS_COLORS[flick.axis],
        )

    def _on_flip(self, flip: Flip) -> None:
        self._last_flip = flip
        self._add_event_row(
            flip.t, "flip", f"from={flip.from_face} to={flip.to_face} axis={flip.axis.upper()}",
            theme.STATUS["warning"],
        )

    def _on_quick_move(self, move: QuickMove) -> None:
        self._last_move = move
        self.tile_move.set_value(
            f"{move.label}   {move.peak_speed:.2f} m/s over "
            f"{move.distance * 100:.0f} cm",
            theme.STATUS["good"],
        )
        self._add_event_row(
            move.t,
            "move",
            f"direction={move.label} speed={move.peak_speed:.2f}m/s "
            f"distance={move.distance * 100:.0f}cm "
            f"peak={move.peak_accel:.1f}m/s2 duration={move.duration_ms:.0f}ms",
            theme.STATUS["good"],
        )

    def _on_estimator_changed(self, index: int) -> None:
        kalman = index == 0
        self.dead_reckoning = (
            self.kalman_estimator if kalman else self.simple_estimator
        )
        self.dead_reckoning.zupt_enabled = self.chk_zupt.isChecked()
        self.dead_reckoning.reset()
        self.traj_view.clear_path()

        # The velocity leak is a property of the simple estimator only. The
        # Kalman filter has no use for one: leaking velocity toward zero at a
        # fixed rate is a crude stand-in for knowing how uncertain the velocity
        # is, and the covariance is that knowledge done properly.
        self.damping_slider.setEnabled(not kalman)
        self.damping_label.setEnabled(not kalman)
        self.tile_drift.set_caption(
            "Position uncertainty (1σ)" if kalman else "Drift since last ZUPT", "m"
        )
        self.estimator_hint.setText(
            "Trapezoidal integration through a 9-state error-state Kalman "
            "filter over position, velocity and accelerometer bias. Standing "
            "still is a measurement rather than a clamp, so each stop also "
            "corrects the position and improves the bias estimate, and the "
            "drawn path is corrected back over the segment that earned it."
            if kalman else
            "Rectangular integration with a hard velocity clamp when still. "
            "Simpler and easier to check, but each stop throws away the "
            "position error instead of using it, so the error only ever grows."
        )

    def _on_flick_directions(self, _index: int = 0) -> None:
        count = DIRECTION_CHOICES[self.flick_directions.currentIndex()][1]
        plane = FLICK_FRAME_CHOICES[self.flick_plane.currentIndex()][1]
        bearings = count is not None and plane is None
        if count is None:
            self.flick_detector.sector_map = None
            self.flick_detector.frame = None
        elif bearings:
            # Degrees from the front: quantise where the flick went, not what
            # it turned about, and measure it clockwise from straight up.
            self.flick_detector.frame = flick_frame(self.flick_front.currentData())
            self.flick_detector.sector_map = flick_bearing_map(count)
        else:
            self.flick_detector.plane = plane
            self.flick_detector.frame = None
            self.flick_detector.sector_map = SectorMap(count)
        # Axis mode divides three axes; the sector modes divide one plane. The
        # dominance floor means different things in each, but the same number
        # works for both, so it is deliberately left alone here.
        self.flick_plane.setEnabled(count is not None)
        self.flick_front.setEnabled(bearings)

    def _on_move_directions(self, _index: int = 0) -> None:
        count = self.move_directions.currentData()
        self.quick_move_detector.sector_map = frame_sector_map(int(count))

    def _on_move_threshold(self, value: int) -> None:
        threshold = value / 10.0
        self.quick_move_detector.on_threshold_ms2 = threshold
        # The event only ends once acceleration has been quiet for a while, so
        # the release threshold has to sit well below the trigger or a gentle
        # move never looks quiet enough to have finished.
        self.quick_move_detector.off_threshold_ms2 = max(0.2, threshold * 0.32)
        self.move_threshold_label.setText(f"{threshold:.1f} m/s²")

    def _on_damping_changed(self, value: int) -> None:
        self.simple_estimator.velocity_damping = value / 100.0
        self.damping_label.setText(f"{value / 100.0:.2f} /s")

    def _on_flick_threshold(self, value: int) -> None:
        self.flick_detector.on_threshold_dps = float(value)
        # Keep the release threshold comfortably below the trigger, or the
        # detector can never see the event end.
        self.flick_detector.off_threshold_dps = max(10.0, value * 0.27)
        self.flick_threshold_label.setText(f"{value} dps")

    def _on_flick_dominance(self, value: int) -> None:
        self.flick_detector.min_dominance = value / 100.0
        self.flick_dominance_label.setText(f"{value / 100.0:.2f}")

    def _reset_origin(self) -> None:
        self.dead_reckoning.reset_origin()
        self.traj_view.clear_path()

    def _on_zupt_toggled(self, on: bool) -> None:
        self.kalman_estimator.zupt_enabled = on
        self.simple_estimator.zupt_enabled = on

    def _reset_dead_reckoning(self) -> None:
        self.dead_reckoning.reset()
        self.flick_detector.reset()
        self.flip_detector.reset()
        self.quick_move_detector.reset()
        self.traj_view.clear_path()

    # ==================================================================
    # Calibration flow
    # ==================================================================
    def _require_connection(self) -> bool:
        if not self.link.connected:
            QMessageBox.information(
                self, "Not connected", "Connect to the board before calibrating."
            )
            return False
        return True

    def _start_gyro_capture(self) -> None:
        if not self._require_connection():
            return
        self._gyro_collector = GyroBiasCollector()
        self.gyro_progress.setValue(0)
        self.gyro_result.setText("Capturing -- keep the board perfectly still.")
        self.gyro_result.setStyleSheet("")
        self._cal_mode = "gyro"

    def _finish_gyro_capture(self) -> None:
        self._cal_mode = None
        bias, spread = self._gyro_collector.result()
        self.cal.gyro_bias = bias

        moved = float(np.max(spread)) > 2.0
        self.gyro_result.setText(
            f"Bias  {bias[0]:+.4f}  {bias[1]:+.4f}  {bias[2]:+.4f} dps"
            f"    peak-to-peak spread {np.max(spread):.3f} dps"
            + ("    -- the board moved during capture, try again" if moved else "")
        )
        self.gyro_result.setStyleSheet(
            f"color: {theme.STATUS['warning'] if moved else theme.STATUS['good']};"
        )
        self.autosave_calibration("gyroscope bias")

    def _start_accel_capture(self, name: str) -> None:
        if not self._require_connection():
            return
        self._accel_collector.start(name)
        self.accel_status[name].setText("capturing...")
        self.accel_progress.setValue(0)
        self._cal_mode = "accel"

    def _finish_accel_capture(self) -> None:
        self._cal_mode = None
        self.accel_progress.setValue(100)
        for name, mean in self._accel_collector.captured.items():
            self.accel_status[name].setText(
                f"{mean[0]:+.3f} {mean[1]:+.3f} {mean[2]:+.3f} g"
            )
            self.accel_status[name].setStyleSheet(f"color: {theme.STATUS['good']};")

        if self._accel_collector.complete:
            bias, scale = self._accel_collector.result()
            self.cal.accel_bias = bias
            self.cal.accel_scale = scale
            summary = (
                f"Bias  {bias[0]:+.4f} {bias[1]:+.4f} {bias[2]:+.4f} g"
                f"    Scale  {scale[0]:.4f} {scale[1]:.4f} {scale[2]:.4f}"
            )
            rejected = self._accel_collector.rejected
            self.accel_result.setText(
                summary if not rejected else summary + "\n" + "\n".join(rejected)
            )
            self.accel_result.setStyleSheet(
                f"color: {theme.STATUS['good' if not rejected else 'serious']};")
            self.autosave_calibration("accelerometer bias and gain")
        else:
            remaining = [
                name for name, *_ in SIX_POSITIONS
                if name not in self._accel_collector.captured
            ]
            self.accel_result.setText("Still needed: " + ", ".join(remaining))

    def _toggle_mag_collection(self) -> None:
        if self._cal_mode == "mag":
            self._cal_mode = None
            self.mag_collect.setText("Start collecting")
            return
        if not self._require_connection():
            return
        self._cal_mode = "mag"
        self.mag_collect.setText("Stop collecting")
        self.mag_result.setText("Rotate the board through as many attitudes as you can.")
        self.mag_result.setStyleSheet("")

    def _fit_mag(self) -> None:
        self._cal_mode = None
        self.mag_collect.setText("Start collecting")

        result = self._mag_collector.fit()
        self._mag_fit = result
        self.mag_result.setText(result.message)
        self.mag_result.setStyleSheet(
            f"color: {theme.STATUS['good'] if result.ok else theme.STATUS['critical']};"
        )
        if not result.ok:
            return

        self.cal.mag_bias = result.bias
        self.cal.mag_soft = result.soft
        self.autosave_calibration("magnetometer iron correction")

        points = self._mag_collector.points
        corrected = (result.soft @ (points - result.bias).T).T
        self.mag_scatter.set_points(points, corrected)

        self.mag_result.setText(
            f"{result.message}\n"
            f"Hard iron  {result.bias[0]:+.2f} {result.bias[1]:+.2f} "
            f"{result.bias[2]:+.2f} uT    "
            f"semi-axes {result.radii[0]:.1f} / {result.radii[1]:.1f} / "
            f"{result.radii[2]:.1f} uT"
        )

    def _clear_mag(self) -> None:
        self._cal_mode = None
        self._mag_collector.clear()
        self._mag_fit = None
        self.mag_collect.setText("Start collecting")
        self.mag_scatter.clear_points()
        self.mag_coverage.setValue(0)
        self.mag_count.setText("0 points")
        self.mag_result.setText("Cleared.")
        self.mag_result.setStyleSheet("")

    def _on_beta_changed(self, value: int) -> None:
        self.fusion.beta = value / 1000.0
        self.beta_label.setText(f"{self.fusion.beta:.3f}")

    def _on_zeta_changed(self, value: int) -> None:
        self.fusion.zeta = value / 10000.0
        self.zeta_label.setText(f"{self.fusion.zeta:.4f}")

    def _apply_mag_config(self) -> None:
        for command in (
            f"mag range {self.mag_range.currentText()}",
            f"mag odr {self.mag_odr.currentText()}",
            f"mag osr1 {self.mag_osr1.currentText()}",
            f"mag osr2 {self.mag_osr2.currentText()}",
            f"mag sr {self.mag_sr.currentText()}",
            f"mag mode {self.mag_mode.currentText()}",
        ):
            self.link.send(command)

    # ------------------------------------------------------------------
    def _refresh_calibration_labels(self) -> None:
        c = self.cal
        self.gyro_result.setText(
            f"Bias  {c.gyro_bias[0]:+.4f}  {c.gyro_bias[1]:+.4f}  "
            f"{c.gyro_bias[2]:+.4f} dps"
        )
        self.accel_result.setText(
            f"Bias  {c.accel_bias[0]:+.4f} {c.accel_bias[1]:+.4f} "
            f"{c.accel_bias[2]:+.4f} g    "
            f"Scale  {c.accel_scale[0]:.4f} {c.accel_scale[1]:.4f} "
            f"{c.accel_scale[2]:.4f}"
        )
        self.mag_result.setText(
            f"Hard iron  {c.mag_bias[0]:+.2f} {c.mag_bias[1]:+.2f} "
            f"{c.mag_bias[2]:+.2f} uT"
        )

    def _push_calibration(self) -> None:
        if not self._require_connection():
            return
        for command in self.cal.to_device_commands():
            self.link.send(command)
        self.console.appendPlainText("> pushed calibration to board")
        self.tabs.setCurrentIndex(4)

    def autosave_calibration(self, what: str = "calibration") -> None:
        """Queue a write of the working copy. Called by anything that measures.

        Public because easy mode calls it at the end of every step: the point
        of the file is that nothing measured is ever only in memory, and that
        is only true if every place that changes the calibration says so.

        Queued rather than written on the spot because several fields often
        land together -- reading a calibration back off the board sets five --
        and a burst of writes would report itself five times for one event. The
        delay is short enough that the file is up to date before anyone could
        act on the step just finished, and :meth:`closeEvent` flushes it.
        """
        self._autosave_reasons.add(what)
        self._autosave_timer.start()

    def _write_autosave(self) -> None:
        what = ", ".join(sorted(self._autosave_reasons)) or "calibration"
        self._autosave_reasons.clear()
        try:
            self.cal.save(AUTOSAVE_PATH)
        except OSError as exc:
            # Reported and swallowed: a full disk is not a reason to lose the
            # step that was just measured as well.
            self.console.appendPlainText(
                f"! could not autosave to {AUTOSAVE_PATH}: {exc}"
            )
            return
        self.console.appendPlainText(f"> autosaved {what} to {AUTOSAVE_PATH}")

    def _save_calibration(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save calibration", str(Path.home() / "bbda_calibration.json"),
            "JSON (*.json)",
        )
        if path:
            self.cal.save(path)
            self.console.appendPlainText(f"> saved calibration to {path}")

    def _load_calibration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load calibration", str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            self.cal = Calibration.load(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        self._refresh_calibration_labels()
        self.refresh_alignment_combo()
        self.console.appendPlainText(f"> loaded calibration from {path}")
        self.autosave_calibration(f"calibration loaded from {path}")

    def _reset_calibration(self) -> None:
        self.cal = Calibration()
        self._refresh_calibration_labels()
        self.refresh_alignment_combo()
        # Deliberately autosaved: resetting is a decision, and it would be a
        # strange one to undo silently at the next start.
        self.autosave_calibration("the reset to identity")

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        self.render_timer.stop()
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._write_autosave()
        self._closing_link = True
        for link in (self.serial_link, self.udp_link):
            if link.connected:
                link.disconnect()
        super().closeEvent(event)
