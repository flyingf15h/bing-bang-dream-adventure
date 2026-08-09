"""Turns the board's IMU stream into flick events for the Godot game.

Why a bridge process exists at all
----------------------------------
Godot has UDP built in and no serial port support whatsoever -- there is no
engine API for a COM port, and adding one means shipping a GDExtension binary
per platform. So the *game* only ever speaks UDP, and this process is what
gives it a choice of transport: it talks to the board over USB serial or over
WiFi, whichever it was pointed at, and re-emits what it found on localhost.

That choice is a real one and neither side is obviously better:

* **Serial** is lossless and needs no network. It pins the board to a cable,
  which for a game that is played by flicking the board around is exactly the
  wrong shape -- but it is the transport that works when WiFi does not, and it
  is the one to debug with.
* **WiFi** frees the board, at the cost of a few milliseconds of jitter and the
  occasional dropped datagram. A dropped sample costs at most one flick, never
  a wrong one, because detection runs over a window of samples and every record
  carries the device timestamp.

Running detection here rather than in GDScript or on the board
-------------------------------------------------------------
:class:`~bbda.motion.FlickDetector` already exists, is tuned, and is what the
dashboard shows on screen. Putting it here means the game and the dashboard
agree about what a flick is, and that tuning it in one place tunes it for both.
The alternative -- porting the detector to GDScript -- would duplicate the one
piece of logic in this project that most needs a single source of truth, and
would still leave the serial problem unsolved.

Wire format, bridge to game
---------------------------
One JSON object per datagram, UTF-8, no trailing newline required. Every record
carries ``v`` (this format's version) and ``type``:

    {"v":1,"type":"hello","transport":"serial","target":"COM7","sectors":6}
    {"v":1,"type":"flick","seq":12,"t":91.42,"host_t":1712.3,"bearing":88.7,
     "sector":1,"strength":0.61,"peak_dps":464.0,"dominance":0.93,
     "duration_ms":92.0}
    {"v":1,"type":"status","connected":true,"rate_hz":198.4,"samples":19840,
     "flicks":12}
    {"v":1,"type":"motion","bearing":88.7,"dps":210.4,"swing":198.1,
     "threshold_dps":150.0}
    {"v":1,"type":"refused","reason":"swing","bearing":91.2,"peak_dps":388.0,
     "duration_ms":104.0,"detail":"mostly a roll -- only 0.41 of the turn ..."}
    {"v":1,"type":"bye"}

``bearing`` is the direction the flick went in degrees clockwise from straight
up, which is the convention :class:`~bbda.motion.FlickFrame` reports and the
one a person describing a hand movement uses. The game converts it to its own
angle convention; see ImuInput.gd. It is sent as a continuous angle rather than
only as a sector index so that the game's own sector layout -- which a chart
can override -- stays the thing that decides which lane was hit, instead of
being quantised twice against a layout this process guessed at.

``motion`` records are the board's *current* rotation rather than a completed
gesture, sent at :attr:`BridgeConfig.motion_hz` so the game can draw an arrow
that follows the board in the hand. They exist because a flick record arrives
only after the flick is over and is refused outright when it was too weak or
too much of a roll -- so on the evidence of flicks alone, a board that is being
waved about and a board that is unplugged look identical. ``dps`` is the whole
rotation rate, ``swing`` the part of it that moved the board's front (the part
``bearing`` describes), and ``threshold_dps`` the rate a flick starts at, so
the game can show how close a movement came without knowing how the detector is
tuned. They are advisory: a game that ignores them plays exactly as before,
which is what keeps this backwards compatible without a version bump.

``refused`` records say that a movement was seen and deliberately not called a
flick, and why. Silence is the worst possible answer to "I flicked and nothing
happened", because it cannot be told apart from a board that is unplugged: both
look like nothing. These cover both halves of that -- a gesture the detector
judged and rejected, and one that never reached the threshold for it to judge,
which the detector cannot report because from inside it nothing occurred.
Nothing is ever scored from one; ``detail`` is a sentence meant to be shown.
"""

from __future__ import annotations

import json
import math
import socket
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt

import numpy as np

from .link import Link, SerialLink, UdpLink
from .motion import (
    FLICK_FRONT_CHOICES,
    FlickDetector,
    FlickRejection,
    flick_bearing_map,
    flick_frame,
    levelled_frame,
)

#: Where the game listens. One above the board's own 3333 so the two are
#: obviously related and cannot collide when board and game share a host.
DEFAULT_GAME_PORT = 3334

#: This is the ``v`` field above. Bump it when a change would confuse an older
#: ImuInput.gd; the game warns and keeps going rather than failing hard.
WIRE_VERSION = 1

#: Fraction of the flick threshold a movement has to reach before a bridge
#: bothers to say it was too weak. Below this it is a hand adjusting its grip,
#: and reporting every one of those would bury the reports that matter.
WEAK_GESTURE_FRACTION = 0.35


def explain_rejection(rejection: FlickRejection, threshold_dps: float) -> str:
    """One line saying why a movement was not a flick, and what to do.

    Phrased for whoever is holding the board rather than for whoever wrote the
    detector: the numbers are there, but so is the tuning flag that moves the
    limit that was missed. A refusal the player cannot act on is barely better
    than the silence it replaced.
    """
    if rejection.reason == "weak":
        return (f"too gentle -- peaked {rejection.peak_dps:.0f} dps, needs "
                f"{threshold_dps:.0f}. Flick from the wrist, or --threshold "
                f"{max(60.0, rejection.peak_dps * 0.8):.0f}")
    if rejection.reason == "swing":
        return (f"mostly a roll -- only {rejection.value:.2f} of the turn swung "
                f"the board's front, needs {rejection.limit:.2f}. If every "
                f"direction does this, --front names the wrong axis; if only "
                f"some do, --swing {max(0.05, rejection.value * 0.9):.2f}")
    if rejection.reason == "margin":
        return (f"landed between two lanes -- margin {rejection.value:.2f} of "
                f"{rejection.limit:.2f}. Aim at a lane centre, or --margin 0 "
                f"and let the game sort it out")
    if rejection.reason == "duration":
        if rejection.duration_ms < rejection.limit:
            return (f"too brief at {rejection.duration_ms:.0f} ms -- that is a "
                    f"knock, not a flick")
        return (f"too slow at {rejection.duration_ms:.0f} ms -- a flick has to "
                f"finish inside {rejection.limit:.0f} ms. This is a wave")
    return (f"refused on {rejection.reason}: {rejection.value:.2f} against "
            f"{rejection.limit:.2f}")


@dataclass
class BridgeConfig:
    """Everything tunable, with the game's defaults already filled in."""

    # --- where the game is -------------------------------------------
    game_host: str = "127.0.0.1"
    game_port: int = DEFAULT_GAME_PORT
    #: Where this listens for the game's debug panel. One above the game's own
    #: port, and loopback only: it can retune detection and write calibration
    #: to the board, which is not something to expose to a network.
    control_port: int = DEFAULT_GAME_PORT + 1

    # --- how the board is asked to stream ------------------------------
    #: 400 rather than 200. The direction is now the integral of the whole
    #: stroke, so the sample count across that stroke is literally the number
    #: of readings the answer is averaged over, and averaging cuts the noise by
    #: its square root: 24 samples instead of 12 is a direction about 40 per
    #: cent steadier for nothing but bandwidth this link has to spare. The
    #: board's gyroscope runs at 800 Hz either way, so the samples exist
    #: whether or not they are asked for.
    rate_hz: int = 400
    calibrated: bool = True

    # --- what counts as a flick ----------------------------------------
    # A flick lasts 60-150 ms, and at 100 Hz that is six samples to describe it
    # with. The direction is what those samples decide, so resolving them badly
    # is not a small error -- it is a flick reported as the wrong lane.
    # Reaching this rate is, deliberately, very nearly the whole test. Every
    # other floor below has been taken down to the point where it only rejects
    # what is physically not a direction, because in play the honest rule is
    # "if the board got up to speed, that was a flick" -- a movement thrown
    # hard enough to be meant and then argued out of existence by a quality
    # test is indistinguishable, from the player's side, from a dead sensor.
    #
    # 110 rather than the 150 this used to be. A wrist flick peaks in the high
    # hundreds and a deliberate but unhurried one still clears 150 comfortably;
    # what did not clear it was the tail of them -- the tired ones, the ones
    # thrown from an awkward grip, the ones a board with a noisy gyro read low.
    # There is a long way down to a hand at rest, which reads under 25 dps even
    # holding something, so this buys a lot of tolerance for very little risk.
    on_threshold_dps: float = 110.0
    off_threshold_dps: float = 40.0
    # In bearing mode this is the swing floor: how much of the rotation moved
    # the board's front rather than twisting about it. At 0.2 it tolerates
    # nearly 80 degrees of roll mixed into a flick, which is to say it now only
    # rejects a movement that is *almost entirely* a roll -- and that one it
    # must, because a roll leaves the front pointing exactly where it was and
    # so has no direction to report. It is the one refusal here that is about
    # physics rather than about confidence.
    min_swing: float = 0.2
    # How far from a sector boundary a flick has to land. Zero: off.
    #
    # The game does not need this test any more. It matches a flick against
    # every note within its aim tolerance and takes the one that best explains
    # it, so a flick that landed between two lanes is something it can resolve
    # -- and resolving it beats refusing it, since a refusal costs the input
    # entirely and looks exactly like the board having missed the movement.
    # Raise it only if flicks are landing in neighbouring lanes often enough
    # that being told "aim again" is genuinely more use than a note being hit.
    min_margin: float = 0.0
    # The return stroke is the reason this is not smaller. Flick the board up
    # and the hand brings it back down, and that return is a real rotation the
    # other way -- with no refractory period it lands as a second flick in the
    # opposite lane. Lower it for faster charts and expect phantom opposites.
    refractory_ms: float = 200.0
    min_duration_ms: float = 15.0
    # How long a movement may last and still be an impulse. Raised from 700,
    # which was refusing the slow end of what people actually throw: a swing
    # made from the elbow rather than the wrist stays above the off threshold
    # for most of a second, and it is a swing by any reading except this one.
    max_duration_ms: float = 1000.0
    # Fraction of its own peak the rotation has to fall to before the flick is
    # called finished and sent. This is the latency knob, and it is the one to
    # reach for if flicks feel like they land late: 0.6 reports about 0.3 of a
    # gesture after the peak where waiting for the off threshold costs 0.5, and
    # it does it with 90% of the stroke integrated rather than one sample.
    # Towards 1.0 is faster and coarser, towards 0 is the old behaviour.
    commit_fraction: float = 0.6
    # Measure directions against gravity rather than against the board's own
    # axes. On means the direction a flick is reported to have gone does not
    # change when the board is held at a different angle, which is the single
    # biggest source of a flick landing in the lane next door. Off restores the
    # board-frame behaviour, and is worth trying only if the accelerometer is
    # so badly calibrated that its idea of vertical is worse than no idea.
    level_with_gravity: bool = True

    # --- how the flick is described to the game ------------------------
    #: Sector count and offset the *detector* gates on. The game re-quantises
    #: the continuous bearing against its own layout, so these only decide
    #: which flicks are rejected as too close to a boundary. The default 30
    #: degree offset centres them on the game's lanes at 0/60/.../300.
    sectors: int = 6
    sector_offset_deg: float = 30.0
    front: str = "+X"
    #: Which hand this board plays, and so which notes its flicks may hit:
    #: ``"left"`` for the blue ones, ``"right"`` for the pink, ``""`` for a
    #: single board that plays everything.
    #:
    #: Carried on every record rather than inferred from which socket it came
    #: down, because both boards post to the same port -- the game has one
    #: listener and one input path, and giving each board its own would
    #: duplicate the half of this that is hardest to keep in step. It is a
    #: property of how the board is being held, so it is set when the bridge is
    #: started and is deliberately not something the game can change: a board
    #: that swapped hands mid-song would be indistinguishable from one that had
    #: started scoring the wrong colour.
    hand: str = ""
    #: Peak rate mapped to strength 1.0. A hard hand flick peaks near 700 dps.
    strength_ceiling_dps: float = 700.0

    # --- how often the live arrow is fed -------------------------------
    #: Rate for ``motion`` records, or 0 to send none. 30 is a display rate,
    #: not a measurement rate: the board still streams at ``rate_hz`` and every
    #: sample is still detected on, but the arrow is only redrawn as fast as it
    #: can be seen moving. Each record summarises the samples since the last
    #: one rather than sampling one of them, so a flick's peak cannot fall in a
    #: gap between records.
    motion_hz: float = 30.0

    verbose: bool = False


class GameBridge:
    """Runs a :class:`FlickDetector` over a link and posts what it finds.

    The link's records arrive on its reader thread. They are handled there
    directly, with ``Qt.DirectConnection``, which is what lets this run without
    a Qt event loop: the default queued connection would post the signal to an
    event loop that a headless bridge does not have, and every sample would sit
    in the queue for ever. Detection is a few microseconds of numpy per sample,
    so doing it on the reader thread costs nothing worth reclaiming.
    """

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig()
        self.detector = self._build_detector()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        #: Where flicks are posted, as (host, port).
        self.peer = (self.config.game_host, self.config.game_port)

        self._link: Optional[Link] = None
        #: What the link was opened against, for repeating hello later.
        self._target = ""
        #: How to find this board again if it goes away: ``(host, udp port)``
        #: over WiFi, ``(None, port)`` over serial. Held per bridge rather than
        #: worked out by whoever is reconnecting, because with two boards there
        #: is no longer one answer -- one may be on a cable and the other on
        #: the network, and reopening the wrong one silently gives both hands
        #: to the same board.
        self.reconnect_target: tuple[Optional[str], int] = (None, 3333)
        self._seq = 0
        self.samples = 0
        self.flicks = 0
        #: Largest |gyro| seen since the last reset, and the newest one, both
        #: in dps. These exist for --monitor: when a flick does not register,
        #: the first thing to establish is whether it ever crossed the
        #: threshold, and that is not something the flick log can show --
        #: a flick that was refused leaves no entry in it at all.
        self.peak_dps_seen = 0.0
        self.last_dps = 0.0
        #: Transport delay on the newest sample and the worst seen since the
        #: last reset, in ms. Shown by --monitor and folded into every flick's
        #: reported lag, because a flick found in a sample that waited 40 ms to
        #: get here happened 40 ms earlier than the rest of the chain thinks.
        self.last_transport_ms = 0.0
        self.peak_transport_ms = 0.0
        self._clock_floor: float | None = None
        self._clock_floor_t = 0.0
        self._rate_window: deque[float] = deque()
        self._started_at = time.monotonic()
        self._last_status = 0.0
        #: Strongest swing seen since the last motion record, and the bearing
        #: it went in. Held rather than sampled so that the arrow shows the
        #: fastest part of a movement: at 200 Hz a 33 ms window holds six or
        #: seven samples, and picking whichever one the timer happened to land
        #: on would make a real flick's peak a coin toss.
        self._motion_swing = 0.0
        self._motion_dps = 0.0
        self._motion_bearing = 0.0
        #: Device time of the last motion record. Negative so the first sample
        #: of a session always sends one, whatever the board's clock reads.
        self._last_motion = -1.0
        #: Host time until which the board's own motion is held back, so that
        #: a simulated gesture is not fought over by a real board reporting
        #: itself still. Only ever set by the flick simulator; zero otherwise.
        self.mute_motion_until = 0.0

        #: Movements that reached the threshold and were refused anyway, and
        #: the last explanation given for one.
        self.refused = 0
        self.last_refusal_text = ""
        self._rejections_seen = 0
        #: State for spotting a movement that never reached the threshold at
        #: all. The detector cannot report those -- from inside it, nothing
        #: happened -- but "I flicked and nothing registered" is most often
        #: exactly this, so it is worth watching for separately.
        self._weak_active = False
        self._weak_peak = 0.0
        self._weak_bearing = 0.0
        self._weak_start_t = 0.0

        #: The debug panel's socket, opened on demand by :meth:`open_control`.
        self._control: Optional[socket.socket] = None
        #: Armed by a "learn_front" command: captures the next real movement so
        #: its peak can be measured against every candidate front axis.
        self._learn: Optional[dict] = None
        #: Armed by "measure_rest": sums samples to find the gyro's bias and
        #: noise while the board is left alone.
        self._rest: Optional[dict] = None
        #: Last bias measured, kept so it can be written to the board without
        #: measuring twice.
        self.last_rest_bias = (0.0, 0.0, 0.0)
        #: The board's own stored gyro bias, as it last reported it.
        self.board_gyro_bias = (0.0, 0.0, 0.0)
        self._bias_write_pending = False
        self._bias_write_deadline = 0.0

        #: Frozen-stream watch. A board whose IMU has stopped being read keeps
        #: sending at the full rate -- the same numbers, for ever. Every other
        #: indicator says the link is perfect, and the only visible symptom is
        #: that flicks never register, which is indistinguishable from a
        #: detector that is tuned wrong. See :meth:`_watch_frozen`.
        self._last_reading: Optional[tuple] = None
        self._identical = 0
        self.stalled = False
        #: True once the accelerometer has been reading something that cannot
        #: be gravity for long enough to be a calibration fault rather than a
        #: hard movement. See :meth:`_watch_gravity`.
        self.gravity_broken = False
        self._bad_gravity = 0
        self.last_status_text = ""
        #: False once the transport has reported a failure, which is what the
        #: reconnect loop watches. Distinct from :attr:`connected`, which asks
        #: the transport: a serial port whose device has vanished can still
        #: report itself open until the next read fails.
        self.link_up = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _build_detector(self) -> FlickDetector:
        cfg = self.config
        return FlickDetector(
            on_threshold_dps=cfg.on_threshold_dps,
            off_threshold_dps=cfg.off_threshold_dps,
            min_duration_ms=cfg.min_duration_ms,
            max_duration_ms=cfg.max_duration_ms,
            refractory_ms=cfg.refractory_ms,
            min_dominance=cfg.min_swing,
            min_margin=cfg.min_margin,
            sector_map=flick_bearing_map(cfg.sectors, cfg.sector_offset_deg),
            frame=flick_frame(cfg.front),
            commit_fraction=cfg.commit_fraction,
            level_with_gravity=cfg.level_with_gravity,
        )

    # ------------------------------------------------------------------
    # The debug panel's control channel
    # ------------------------------------------------------------------
    def open_control(self) -> bool:
        """Start listening for the game's debug panel. Failure is survivable.

        Every knob this exposes lives here rather than in the game because the
        detector does, and a panel that edited its own copy of the tuning would
        be showing numbers that nothing acts on. So the panel sends changes
        here, and what it displays is this process reporting back what it is
        actually running -- there is one set of values, and it is this one.
        """
        try:
            self._control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._control.bind(("127.0.0.1", self.config.control_port))
            self._control.setblocking(False)
            return True
        except OSError as error:
            self._control = None
            print(f"[control] port {self.config.control_port} unavailable "
                  f"({error}); the in-game debug panel will not be able to "
                  f"change anything. Another bridge is probably running.")
            return False

    def poll_control(self) -> None:
        """Handle whatever the panel has sent. Never blocks."""
        if self._control is None:
            return
        while True:
            try:
                data, _ = self._control.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            try:
                message = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(message, dict):
                self._handle_command(message)

    def _handle_command(self, message: dict) -> None:
        command = str(message.get("cmd", ""))
        if command == "get":
            # Hello as well as config: it carries the transport, and a game
            # that started after the bridge missed the one sent at connect.
            # Without it the panel cannot tell demo mode from a dead board.
            self.send_hello(getattr(self, "_transport", "")
                            or (self._link.kind if self._link else "demo"),
                            self._target or "none")
            self.send_config()
        elif command == "set":
            self.apply_tuning(message)
        elif command == "learn_front":
            self._arm_learn(float(message.get("expect_bearing", 0.0)))
        elif command == "measure_rest":
            self._arm_rest(float(message.get("seconds", 2.0)))
        elif command == "write_bias":
            self.write_bias_to_board()
        elif command == "reset":
            self.detector.reset()
            self.send_config()

    #: Tuning the panel is allowed to change, as wire name -> config attribute.
    #: Explicit rather than "any attribute of config", so a typo in a datagram
    #: cannot quietly redirect the game's output or reopen the transport.
    TUNABLE = {
        "front": "front",
        "on_threshold_dps": "on_threshold_dps",
        "off_threshold_dps": "off_threshold_dps",
        "min_swing": "min_swing",
        "min_margin": "min_margin",
        "refractory_ms": "refractory_ms",
        "min_duration_ms": "min_duration_ms",
        "max_duration_ms": "max_duration_ms",
        "sector_offset_deg": "sector_offset_deg",
        "strength_ceiling_dps": "strength_ceiling_dps",
        "calibrated": "calibrated",
        "commit_fraction": "commit_fraction",
        "level_with_gravity": "level_with_gravity",
    }

    def tuning(self) -> dict:
        """Everything the panel can change, as it currently stands."""
        return {name: getattr(self.config, attribute)
                for name, attribute in self.TUNABLE.items()}

    def apply_tuning(self, message: dict) -> None:
        """Take the knobs out of ``message`` and rebuild the detector."""
        changed = []
        for name, attribute in self.TUNABLE.items():
            if name not in message:
                continue
            value = message[name]
            current = getattr(self.config, attribute)
            if name == "front":
                if value not in FLICK_FRONT_CHOICES:
                    continue
            elif isinstance(current, bool):
                value = bool(value)
            else:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            if value != current:
                setattr(self.config, attribute, value)
                changed.append(f"{name}={value}")

        if not changed:
            self.send_config()
            return

        # Rebuilt rather than mutated: the detector reads several of these when
        # it is constructed (the sector map is built from two of them), so
        # setting an attribute on the live one would leave it half retuned.
        self.detector = self._build_detector()
        self.detector.reset()
        self._rejections_seen = self.detector.rejections
        if self.config.calibrated is not None and self._link is not None:
            self._link.send("csvcal on" if self.config.calibrated else "csvcal off")
        if self.config.verbose:
            print("[control] " + "  ".join(changed))
        self.send_config()

    def send_config(self) -> None:
        """Tell the panel what is actually running."""
        self._emit({"type": "config", "control_port": self.config.control_port,
                    "sectors": self.config.sectors, **self.tuning()})

    # ------------------------------------------------------------------
    # Guided measurements
    # ------------------------------------------------------------------
    def _arm_learn(self, expect_bearing: float) -> None:
        self._learn = {
            "expect": expect_bearing % 360.0,
            "active": False,
            "peak": 0.0,
            "vector": np.zeros(3),
            "turn": np.zeros(3),
            "up": None,
            "last_t": None,
        }
        self._emit({"type": "learning", "expect_bearing": expect_bearing % 360.0})

    def _watch_learn(self, device_t: float, rate: float, gyro) -> None:
        """Capture one movement, then say which front axis explains it.

        The player is asked to flick in a direction they name, so the answer is
        known before the measurement: whichever front axis turns the rotation
        they just made into the bearing they said is the right one. That is a
        far better question to put to somebody than "which way does the board's
        X axis point", which needs the silkscreen and a right hand and is the
        step everybody gets wrong.
        """
        state = self._learn
        if state is None:
            return
        if rate >= self.config.on_threshold_dps:
            if not state["active"]:
                state["active"] = True
                # Freeze the vertical at the start of the movement, exactly as
                # a flick does, so the axis this suggests is the axis that will
                # be right when the flick detector uses it.
                up = self.detector.gravity.up
                state["up"] = None if up is None else np.asarray(up).copy()
            if rate > state["peak"]:
                state["peak"] = rate
                state["vector"] = np.asarray(gyro, dtype=float).copy()
            last = state["last_t"]
            if last is not None and 0.0 < device_t - last < 0.2:
                state["turn"] = (state["turn"]
                                 + np.asarray(gyro, dtype=float)
                                 * (device_t - last))
            state["last_t"] = device_t
            return
        if not state["active"] or rate > self.config.off_threshold_dps:
            return

        self._learn = None
        # Judged on the whole movement, not on its fastest sample, for the same
        # reason the detector is: the fastest sample is one 5 ms slice and the
        # question being asked -- which axis explains where this went -- is
        # about the movement.
        peak = state["turn"]
        if float(np.linalg.norm(peak)) < 1e-6:
            peak = state["vector"]
        expect = state["expect"]
        up = state["up"]
        candidates = []
        for front in FLICK_FRONT_CHOICES:
            frame = flick_frame(front)
            if up is not None:
                frame = levelled_frame(frame.front, up, frame.up)
            swing = float(frame.swing_fraction(peak))
            bearing = float(frame.bearing_deg(peak))
            error = abs((bearing - expect + 180.0) % 360.0 - 180.0)
            candidates.append({"front": front, "bearing": round(bearing, 1),
                               "swing": round(swing, 3),
                               "error_deg": round(error, 1)})

        # A front the movement barely swung about cannot be judged on its
        # bearing: the direction of a near-zero vector is noise, and one of
        # those will occasionally post a very low error by chance.
        usable = [c for c in candidates if c["swing"] >= 0.5] or candidates
        best = min(usable, key=lambda c: c["error_deg"])
        candidates.sort(key=lambda c: c["error_deg"])
        self._emit({
            "type": "front_suggestion",
            "front": best["front"],
            "error_deg": best["error_deg"],
            "swing": best["swing"],
            "expect_bearing": expect,
            "peak_dps": round(float(state["peak"]), 1),
            "current": self.config.front,
            "candidates": candidates,
        })
        if self.config.verbose:
            print(f"[learn] flick towards {expect:.0f}deg looks like "
                  f"--front {best['front']} (off by {best['error_deg']:.1f}deg)")

    def _arm_rest(self, seconds: float) -> None:
        # Timed on the board's clock rather than counted in samples. A sample
        # count needs a rate to convert from, and the rate is measured over the
        # last second -- which at the moment a panel first asks for this can
        # easily be zero, and a window of "zero seconds' worth of samples"
        # finishes on the first one and calls whatever it caught the answer.
        self._rest = {"until": None, "seconds": max(0.05, seconds),
                      "sum": np.zeros(3), "peak": 0.0, "n": 0}
        self._emit({"type": "measuring", "seconds": seconds})

    def _watch_rest(self, device_t: float, rate: float, gyro) -> None:
        """Average the gyro while the board is still, to find its bias.

        Bias is what a gyro reads when nothing is moving, and it is the whole
        of "IMU accuracy" as far as this game is concerned: it does not make
        flicks harder to detect, but it never stops, so it is what drifts an
        orientation and what makes a board at rest look like it is creeping.
        """
        state = self._rest
        if state is None:
            return
        if state["until"] is None:
            state["until"] = device_t + state["seconds"]
        state["sum"] = state["sum"] + np.asarray(gyro, dtype=float)
        state["peak"] = max(state["peak"], rate)
        state["n"] += 1
        if device_t < state["until"]:
            return

        self._rest = None
        bias = state["sum"] / max(1, state["n"])
        self.last_rest_bias = tuple(float(v) for v in bias)
        magnitude = float(np.linalg.norm(bias))
        # Thresholds from what this part actually does: a calibrated ICM-45605
        # sitting still reads a few tenths of a dps. Past a couple of dps the
        # board was moved during the measurement, or the stored calibration is
        # wrong for this temperature.
        if state["peak"] > 20.0:
            verdict = "moved"
        elif magnitude < 0.5:
            verdict = "good"
        elif magnitude < 2.0:
            verdict = "fair"
        else:
            verdict = "poor"
        self._emit({
            "type": "rest",
            "verdict": verdict,
            "bias": [round(v, 3) for v in self.last_rest_bias],
            "bias_dps": round(magnitude, 3),
            "peak_dps": round(float(state["peak"]), 2),
            "samples": state["n"],
        })

    def write_bias_to_board(self) -> None:
        """Push the measured bias into the board's stored calibration.

        Written to the board rather than corrected here so that it holds for
        everything the board talks to -- the dashboard, and the game after the
        next reboot -- rather than only for as long as this process runs.

        Asynchronous, because it has to read before it writes: the board's
        ``cal gyro`` *replaces* the stored bias, while what was just measured
        is the residual left over after the stored one was already applied. So
        the two have to be added, which means asking the board what it is
        currently holding and finishing the job when it answers, in
        :meth:`_on_info`.
        """
        if self._link is None or not self.connected:
            self._emit({"type": "bias_written", "ok": False,
                        "detail": "no board connected"})
            return
        if self.last_rest_bias == (0.0, 0.0, 0.0):
            self._emit({"type": "bias_written", "ok": False,
                        "detail": "measure the rest bias first"})
            return
        self._bias_write_pending = True
        self._bias_write_deadline = time.monotonic() + 3.0
        self._link.send("cal show")

    def _finish_bias_write(self, stored: tuple[float, float, float]) -> None:
        self._bias_write_pending = False
        residual = self.last_rest_bias
        total = tuple(s + r for s, r in zip(stored, residual))
        self._link.send("cal gyro %.5f %.5f %.5f" % total)
        self._link.send("cal save")
        self.board_gyro_bias = total
        # Measuring again would now read close to zero, which is the point --
        # and is also why this converges rather than oscillating if it is run
        # twice.
        self.last_rest_bias = (0.0, 0.0, 0.0)
        detail = ("wrote %+.3f %+.3f %+.3f dps and saved" % total)
        self._emit({"type": "bias_written", "ok": True, "detail": detail,
                    "gyro_bias": [round(v, 5) for v in total]})
        if self.config.verbose:
            print(f"[control] {detail}")

    def _on_info(self, key: str, value: str) -> None:
        """Board key/value replies. Only the stored calibration matters here."""
        if key != "cal.gyro_bias":
            return
        try:
            stored = tuple(float(part) for part in value.split()[:3])
        except ValueError:
            return
        if len(stored) != 3:
            return
        self.board_gyro_bias = stored
        self._emit({"type": "board_cal", "gyro_bias": [round(v, 5) for v in stored]})
        if self._bias_write_pending:
            self._finish_bias_write(stored)

    def expire_bias_write(self) -> None:
        """Give up on a board that never answered ``cal show``."""
        if not self._bias_write_pending:
            return
        if time.monotonic() < self._bias_write_deadline:
            return
        self._bias_write_pending = False
        self._emit({"type": "bias_written", "ok": False,
                    "detail": "the board did not report its calibration"})

    def attach(self, link: Link) -> None:
        """Take records from ``link`` from now on."""
        self._link = link
        link.sample.connect(self._on_sample, Qt.ConnectionType.DirectConnection)
        link.status.connect(self._on_status, Qt.ConnectionType.DirectConnection)
        link.info.connect(self._on_info, Qt.ConnectionType.DirectConnection)

    def open(self, link: Link, target: str) -> bool:
        """Attach, connect, and put the board into the mode this needs.

        Safe to call again to reconnect: any previous link is torn down first,
        so a reconnect cannot leave the old port open or its reader thread
        feeding samples into the detector alongside the new one.
        """
        if self._link is not None:
            try:
                self._link.disconnect()
            except Exception:
                pass            # it is already broken; that is why we are here
            self._link = None

        self.attach(link)
        if not link.connect(target):
            return False
        self._target = target

        # Link.connect() has already sent `mode csv` and `rate 100`; override
        # the rate, and ask the board to apply its own stored calibration so
        # the numbers here match what the dashboard shows.
        link.send(f"rate {self.config.rate_hz}")
        link.send("csvcal on" if self.config.calibrated else "csvcal off")
        # Asked for at every open so the panel can show the board's stored
        # calibration, and so a bias write has a fresh number to add to.
        link.send("cal show")

        self.link_up = True
        # Forget how the two clocks lined up last time. The board's microsecond
        # counter restarts from nothing when it reboots, and a reboot is the
        # usual reason this is being called again -- so the offset between the
        # board's clock and the host's jumps by however long the board had been
        # up. Kept, the old floor would make every sample of the new session
        # look hours late, and every flick would be backdated by that much and
        # score against a moment long gone.
        self._clock_floor = None
        self._clock_floor_t = 0.0
        self.last_transport_ms = 0.0
        self.peak_transport_ms = 0.0
        self.detector.reset()
        self.send_hello(link.kind, target)
        self.send_config()
        return True

    @property
    def target(self) -> str:
        """What this bridge last opened -- a port name, or a host over WiFi."""
        return self._target

    @property
    def connected(self) -> bool:
        return self._link is not None and self._link.connected

    def close(self) -> None:
        self._emit({"type": "bye"})
        if self._link is not None:
            self._link.disconnect()
            self._link = None
        self.link_up = False

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------
    def _emit(self, payload: dict) -> None:
        # The hand goes on everything this bridge sends, not just on flicks.
        # Two bridges share one port, so without it the game cannot tell which
        # board a refusal, a status or a live motion record came from -- and
        # "one of your two boards has stopped" is not a useful thing to be
        # told. Never overwritten, so a payload that names its own hand wins.
        if self.config.hand:
            payload = {"hand": self.config.hand, **payload}
        payload = {"v": WIRE_VERSION, **payload}
        try:
            self._socket.sendto(
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                self.peer,
            )
        except OSError:
            # Nothing is listening yet, or the game just quit. On Windows an
            # unreachable port comes back as an error on a *later* send rather
            # than this one, so this is not a reliable liveness check and is
            # deliberately not treated as one -- the game may well start after
            # the bridge and must be able to just begin receiving.
            pass

    def send_hello(self, transport: str, target: str) -> None:
        self._transport = transport
        self._emit({
            "type": "hello",
            "transport": transport,
            "target": target,
            "sectors": self.config.sectors,
            "rate_hz": self.config.rate_hz,
        })

    def _note_motion(self, gyro) -> None:
        """Fold one sample into the next motion record.

        Scalar arithmetic, and the bearing computed only when it is going to be
        used. This runs on every sample purely to feed a 30 Hz arrow, so it has
        no business being the most expensive thing on the reader thread -- and
        with a numpy cross product, a numpy norm and a rebuilt frame per call,
        it was.
        """
        # The levelled frame, the same one a flick would be judged in. The
        # arrow and the flick have to agree about which way is up or the arrow
        # stops being a way of aiming: it would point where the board thinks
        # the movement went while the score came from where gravity says it
        # did, and the player would learn to aim by an arrow that lies.
        frame = self.detector.live_frame() or self.detector.frame
        gx, gy, gz = float(gyro[0]), float(gyro[1]), float(gyro[2])
        fx, fy, fz = frame.front
        # sweep = gyro x front, the velocity of the board's front.
        sx, sy, sz = (gy * fz - gz * fy, gz * fx - gx * fz, gx * fy - gy * fx)
        swing = math.sqrt(sx * sx + sy * sy + sz * sz)
        if swing > self._motion_swing:
            self._motion_swing = swing
            # Only re-read the bearing when the swing grew. A bearing taken
            # from a nearly-still board is the direction of its noise, and
            # letting that overwrite the direction of a real movement is what
            # would make the arrow jitter while the hand is steady.
            self._motion_bearing = float(frame.bearing_deg(gyro))
        rate = math.sqrt(gx * gx + gy * gy + gz * gz)
        if rate > self._motion_dps:
            self._motion_dps = rate

    def _maybe_send_motion(self, device_t: float) -> None:
        """Send a motion record if one is due on the *board's* clock.

        The device clock rather than the host's, for two reasons. It is even
        across the whole stream, where host arrival times carry USB and WiFi
        scheduling jitter that would make the arrow's updates bunch up. And on
        Windows ``time.monotonic`` ticks every 15.6 ms, which against a 33 ms
        interval is a third of a frame of slop -- enough to see in something
        drawn every frame, and enough to make a test of this rate meaningless.
        """
        if self.config.motion_hz <= 0.0:
            return
        if time.monotonic() < self.mute_motion_until:
            # A simulated gesture is in flight. Drop what the board reported
            # rather than holding it: it is a second of the board lying still
            # on a desk, and sending it the moment the mute lifts would snap
            # the arrow back mid-swing.
            self._motion_swing = 0.0
            self._motion_dps = 0.0
            return
        elapsed = device_t - self._last_motion
        # A board that resets starts its clock again from zero, which leaves
        # elapsed negative. Treat that as due rather than as "not yet", or the
        # arrow would stay frozen until the board had been running as long as
        # it had before the reset.
        if 0.0 <= elapsed < 1.0 / self.config.motion_hz:
            return
        self._last_motion = device_t
        self._emit({
            "type": "motion",
            "bearing": round(self._motion_bearing, 1),
            "dps": round(self._motion_dps, 1),
            "swing": round(self._motion_swing, 1),
            "threshold_dps": self.config.on_threshold_dps,
        })
        # Start the next window empty rather than decaying this one, so the
        # arrow falls back to rest on its own when the board is put down.
        self._motion_swing = 0.0
        self._motion_dps = 0.0

    def _maybe_send_status(self, now: float) -> None:
        if now - self._last_status < 1.0:
            return
        self._last_status = now
        self._emit({
            "type": "status",
            "connected": True,
            "rate_hz": round(self.sample_rate, 1),
            "samples": self.samples,
            "flicks": self.flicks,
        })

    # ------------------------------------------------------------------
    # Incoming
    # ------------------------------------------------------------------
    def _on_status(self, message: str, connected: bool) -> None:
        self.last_status_text = message
        # Only a link that was up can go down. Opening a transport begins by
        # tearing down any previous one, which emits a "Disconnected" before
        # anything has connected -- forwarding that would have the game warn
        # about a board going away every single time one arrives.
        if not connected and self.link_up:
            self.link_up = False
            self._emit({"type": "status", "connected": False, "detail": message})
        if self.config.verbose:
            print(f"[link] {message}")

    @property
    def sample_rate(self) -> float:
        """Samples per second over the last second, measured not assumed."""
        return float(len(self._rate_window))

    #: How fast the transport-delay floor is allowed to drift upwards, in
    #: seconds of offset per second of running. The board's crystal and the
    #: host's disagree by tens of parts per million, so the raw difference
    #: between their clocks walks steadily in one direction; without a creep
    #: the floor would latch onto the first sample of the session for ever and
    #: every later delay would read as that drift rather than as queueing. Two
    #: hundred parts per million is comfortably faster than any real crystal
    #: pair and still far slower than the delays being measured, so it tracks
    #: the drift without absorbing a single one of them.
    CLOCK_DRIFT_PER_S = 200e-6

    def _transport_delay(self, sample) -> float:
        """How long this sample spent getting here, in seconds.

        Only the *difference* between the two clocks is knowable -- neither one
        is a reference -- so the luckiest sample seen recently is taken as
        having had no delay at all and everything else is measured above it.
        That makes this a floor on the true delay rather than the delay itself,
        which is the honest thing to claim and is exactly what is wanted: it
        cannot invent latency that is not there.
        """
        host_t = float(getattr(sample, "host_t", 0.0) or 0.0)
        if host_t <= 0.0:
            return 0.0             # replayed or synthetic; nothing to measure
        offset = host_t - float(sample.t)
        if self._clock_floor is None:
            self._clock_floor = offset
            self._clock_floor_t = host_t
            return 0.0
        elapsed = max(0.0, host_t - self._clock_floor_t)
        self._clock_floor_t = host_t
        self._clock_floor += self.CLOCK_DRIFT_PER_S * elapsed
        if offset < self._clock_floor:
            self._clock_floor = offset
        return max(0.0, offset - self._clock_floor)

    def _on_sample(self, sample) -> None:
        now = time.monotonic()
        self.samples += 1

        self.last_transport_ms = self._transport_delay(sample) * 1000.0
        if self.last_transport_ms > self.peak_transport_ms:
            self.peak_transport_ms = self.last_transport_ms

        rate = float((sample.gyro ** 2).sum() ** 0.5)
        self.peak_dps_seen = max(self.peak_dps_seen, rate)
        self.last_dps = rate

        # A deque popped from the front, not a list rebuilt by comprehension.
        # Once the window is full the old form rebuilt a 400-element list on
        # *every* sample, which is 160 000 list operations a second to answer
        # "how many samples in the last second" -- a question that only needs
        # the stale ones dropped.
        self._rate_window.append(now)
        cutoff = now - 1.0
        while self._rate_window and self._rate_window[0] < cutoff:
            self._rate_window.popleft()

        # The detector is driven by the *device* clock. Host arrival times
        # carry USB and WiFi scheduling jitter, and a duration measured
        # through that jitter is what decides whether a flick is a flick.
        self._note_motion(sample.gyro)
        self._watch_frozen(sample.gyro, sample.accel)
        self._watch_gravity(sample.accel)
        self._watch_weak_gesture(float(sample.t), rate, sample.gyro)
        if self._learn is not None:
            self._watch_learn(float(sample.t), rate, sample.gyro)
        if self._rest is not None:
            self._watch_rest(float(sample.t), rate, sample.gyro)

        flick = self.detector.update(sample.t, sample.gyro, sample.accel)
        if flick is not None:
            self._publish(flick, now)
        elif self.detector.rejections != self._rejections_seen:
            self._rejections_seen = self.detector.rejections
            if self.detector.last_rejection is not None:
                self._publish_refusal(self.detector.last_rejection)

        self._maybe_send_motion(float(sample.t))
        self._maybe_send_status(now)

    #: Identical readings in a row before the stream is called frozen. At 200 Hz
    #: this is a second and a half. A real sensor cannot repeat a full six-axis
    #: reading to the last bit even once -- the noise floor guarantees the low
    #: bits move -- so this cannot fire on a board that is merely being held
    #: very still, however long it is held.
    FROZEN_SAMPLES = 300

    def _watch_frozen(self, gyro, accel) -> None:
        """Notice a board that is sending, but no longer measuring.

        This is the failure that wasted the most time: the port opens, the rate
        is a healthy 200 Hz, the sample count climbs, and every reading is the
        same stale frame. Nothing else in the chain can tell -- the detector is
        being fed perfectly good samples of a board that is not moving, so it
        correctly reports no flicks, for ever.

        On this hardware the usual cause is documented: the IMU's APEX engine
        initialises once per *IMU* power-on, and a CPU reset leaves the sensor
        powered and holding its state. Which is why the advice is to unplug the
        cable rather than to press reset -- a distinction nobody would guess.
        """
        reading = (tuple(float(v) for v in gyro), tuple(float(v) for v in accel))
        if reading != self._last_reading:
            self._last_reading = reading
            self._identical = 0
            if self.stalled:
                self.stalled = False
                self._emit({"type": "status", "connected": True,
                            "stalled": False,
                            "detail": "the board is measuring again"})
                if self.config.verbose:
                    print("[link] readings are changing again")
            return

        self._identical += 1
        if self.stalled or self._identical < self.FROZEN_SAMPLES:
            return
        self.stalled = True
        detail = ("the board is streaming but its readings never change -- "
                  "the IMU has stopped being read, so no flick can register. "
                  "Unplug the cable and plug it back in; a reset is not enough, "
                  "because it leaves the sensor powered and holding its state.")
        self._emit({"type": "status", "connected": True, "stalled": True,
                    "detail": detail})
        print("[link] FROZEN: " + detail)

    #: Samples in a row whose magnitude is nowhere near gravity before the
    #: accelerometer is called broken. Half a second at 400 Hz -- long enough
    #: that no amount of shaking reaches it, since a real board passes through
    #: 1 g constantly while being waved about.
    BAD_GRAVITY_SAMPLES = 200
    #: How far |accel| may sit from 1 g and still be a plausible reading of a
    #: board in motion. Generous: a hard flick genuinely pulls two or three g.
    BAD_GRAVITY_TOLERANCE = 3.0

    def _watch_gravity(self, accel) -> None:
        """Notice an accelerometer that cannot be measuring gravity.

        Directions are measured against vertical and vertical comes from the
        accelerometer, so a board whose accelerometer is miscalibrated does not
        fail loudly -- it silently stops levelling, falls back to the board's
        own axes, and goes back to reporting whichever way the grip happens to
        be twisted. Every indicator stays green and flicks simply land in the
        wrong lane, which is indistinguishable from the detector being tuned
        badly and is the reason this is worth watching for by name.

        The case this was written for: a stored per-axis gain of 26 and 75,
        from a six-position calibration where two of the positions were the
        same face. The part read 3.7 g sitting still.
        """
        magnitude = float(np.linalg.norm(np.asarray(accel, dtype=float)))
        if abs(magnitude - 1.0) <= self.BAD_GRAVITY_TOLERANCE:
            self._bad_gravity = 0
            if self.gravity_broken:
                self.gravity_broken = False
                self._emit({"type": "status", "connected": True,
                            "gravity_ok": True,
                            "detail": "the accelerometer reads gravity again"})
            return

        self._bad_gravity += 1
        if self.gravity_broken or self._bad_gravity < self.BAD_GRAVITY_SAMPLES:
            return
        self.gravity_broken = True
        detail = (
            f"the accelerometer reads {magnitude:.1f} g while the board is "
            f"being held, and gravity is 1 g. Its stored calibration is wrong "
            f"-- almost always a six-position capture where two of the "
            f"positions were the same face, which stores a per-axis gain of "
            f"twenty or more. Directions are measured against vertical, so "
            f"until this is fixed they follow however the board is being held "
            f"instead. Run `cal ascale 1 1 1` then `cal save` on the board to "
            f"undo it, or redo the six-position step properly."
        )
        self._emit({"type": "status", "connected": True, "gravity_ok": False,
                    "detail": detail})
        print("[cal] BROKEN ACCELEROMETER: " + detail)

    def _watch_weak_gesture(self, device_t: float, rate: float, gyro) -> None:
        """Notice movements that never reached the flick threshold.

        The detector only wakes at ``on_threshold_dps``, so a gesture that
        peaked below it produces no event and no rejection -- from the
        detector's point of view the board was still. That is the most common
        reason a real flick "does nothing", and the hardest to guess at, so it
        is watched here instead: from the moment the board starts moving at all
        until it stops again.
        """
        cfg = self.config
        if rate >= cfg.off_threshold_dps:
            if not self._weak_active:
                self._weak_active = True
                self._weak_peak = 0.0
                self._weak_start_t = device_t
            if rate > self._weak_peak:
                self._weak_peak = rate
                frame = self.detector.live_frame()
                self._weak_bearing = (float("nan") if frame is None
                                      else float(frame.bearing_deg(gyro)))
            return

        if not self._weak_active:
            return
        self._weak_active = False
        peak = self._weak_peak
        if peak >= cfg.on_threshold_dps:
            return          # the detector saw this one; it has already ruled
        if peak < cfg.on_threshold_dps * WEAK_GESTURE_FRACTION:
            return          # a hand shifting its grip, not an attempted flick
        self._publish_refusal(FlickRejection(
            t=device_t,
            reason="weak",
            peak_dps=peak,
            duration_ms=(device_t - self._weak_start_t) * 1000.0,
            bearing_deg=self._weak_bearing,
            value=peak,
            limit=cfg.on_threshold_dps,
        ))

    def _publish_refusal(self, rejection: FlickRejection) -> None:
        """Tell the game, and whoever is watching the console, about a refusal.

        Sent as its own record rather than as a flick with a flag: the game
        must never score one, and a record type it can only display is a much
        harder thing to get wrong than a flick field somebody forgets to check.
        """
        self.refused += 1
        self.last_refusal_text = explain_rejection(
            rejection, self.config.on_threshold_dps)
        payload = {
            "type": "refused",
            "reason": rejection.reason,
            "peak_dps": round(rejection.peak_dps, 1),
            "duration_ms": round(rejection.duration_ms, 1),
            "detail": self.last_refusal_text,
        }
        # Left out rather than sent as null when there is no frame to measure a
        # direction against: the game tests for the key's presence, and a null
        # would read as a flick towards bearing zero.
        if rejection.bearing_deg == rejection.bearing_deg:
            payload["bearing"] = round(rejection.bearing_deg, 2)
        self._emit(payload)
        if self.config.verbose:
            bearing = rejection.bearing_deg
            where = ("        " if bearing != bearing
                     else f"{bearing:6.1f}deg ")
            print(f"[refused] {where} {self.last_refusal_text}")

    def _publish(self, flick, host_now: float) -> None:
        self._seq += 1
        self.flicks += 1

        # Taken off the flick rather than recomputed here. The frame a bearing
        # is read in is levelled against gravity when the flick starts and is
        # gone by now, so measuring the same rotation against the detector's
        # current frame would answer a different question and quietly disagree
        # with the one the detector actually judged.
        bearing = float(flick.bearing_deg)
        if bearing != bearing:                       # NaN: no frame configured
            bearing = (flick.sector.angle_deg if flick.sector is not None
                       else 0.0)

        # How long ago the flick actually happened, in two parts.
        #
        # The first is detection: a flick cannot be named until enough of it
        # has been seen, so the record is always behind the peak -- by about
        # 0.3 of the gesture, which the player varies freely and so cannot be
        # calibrated out as a constant.
        #
        # The second is transport. The sample this was found in spent some time
        # getting here, and on a bad read that was a tenth of a second. It is
        # invisible from the device clock -- every timestamp in the record is
        # the board's, and they are all equally stale -- so it has to be added
        # here or it is never accounted for anywhere.
        #
        # The game subtracts the total to judge the flick against when it
        # happened rather than when the news of it arrived.
        detect_ms = max(0.0, (float(flick.t) - float(flick.peak_t)) * 1000.0)
        transport_ms = max(0.0, self.last_transport_ms)
        lag_ms = detect_ms + transport_ms

        self._emit({
            "type": "flick",
            "seq": self._seq,
            "t": round(float(flick.t), 4),
            "peak_t": round(float(flick.peak_t), 4),
            "lag_ms": round(lag_ms, 1),
            "detect_ms": round(detect_ms, 1),
            "transport_ms": round(transport_ms, 1),
            "host_t": round(host_now - self._started_at, 4),
            "bearing": round(float(bearing), 2),
            "sector": int(flick.sector.index) if flick.sector is not None else -1,
            "strength": round(self._strength(flick), 3),
            "peak_dps": round(float(flick.peak_dps), 1),
            "dominance": round(float(flick.dominance), 3),
            "duration_ms": round(float(flick.duration_ms), 1),
            # How much of the stroke the direction was averaged over. A flick
            # named off two or three samples is one the sample rate could not
            # resolve, and that is worth being able to see from the game side
            # rather than only from here.
            "turn_deg": round(float(flick.rotation_deg), 1),
            "samples": int(flick.samples),
        })

        if self.config.verbose:
            print(
                f"[flick] #{self._seq:<4} bearing {bearing:6.1f}deg  "
                f"sector {flick.sector.index if flick.sector else '-'}  "
                f"{flick.peak_dps:6.0f} dps  {flick.duration_ms:5.1f} ms  "
                f"turn {flick.rotation_deg:5.1f}deg/{flick.samples:2d}sa  "
                f"swing {flick.dominance:.2f}  "
                f"lag {lag_ms:4.0f} ms ({detect_ms:.0f}+{transport_ms:.0f})"
            )

    def _strength(self, flick) -> float:
        cfg = self.config
        span = max(1.0, cfg.strength_ceiling_dps - cfg.on_threshold_dps)
        return float(min(1.0, max(0.0, (flick.peak_dps - cfg.on_threshold_dps) / span)))

    # ------------------------------------------------------------------
    # Offline testing
    # ------------------------------------------------------------------
    def send_demo_flick(self, bearing_deg: float, strength: float = 0.8) -> None:
        """Post a made-up flick, so the game half can be tested with no board.

        This is the first thing to reach for when flicks are not landing: if
        the game reacts to these, the fault is upstream of this process --
        board, cable, WiFi or detector tuning -- and if it does not, the fault
        is the socket, the port number or the game's own wiring.
        """
        self._seq += 1
        self.flicks += 1
        self._emit({
            "type": "flick",
            "seq": self._seq,
            "t": round(time.monotonic() - self._started_at, 4),
            "host_t": round(time.monotonic() - self._started_at, 4),
            "peak_t": round(time.monotonic() - self._started_at, 4),
            "lag_ms": 0.0,
            "bearing": round(float(bearing_deg) % 360.0, 2),
            "sector": -1,
            "strength": round(float(strength), 3),
            "peak_dps": 400.0,
            "dominance": 1.0,
            "duration_ms": 90.0,
            "demo": True,
        })

    def send_demo_motion(self, bearing_deg: float, swing_dps: float) -> None:
        """Post a made-up motion record, to move the arrow with no board.

        Demo mode sends a rising and falling run of these around each fake
        flick. Without them the arrow would only ever be tested by the flicks
        themselves, and the half of it that follows the board -- which is most
        of what a player sees -- could be broken with every offline check
        still passing.
        """
        self._emit({
            "type": "motion",
            "bearing": round(float(bearing_deg) % 360.0, 1),
            "dps": round(float(swing_dps), 1),
            "swing": round(float(swing_dps), 1),
            "threshold_dps": self.config.on_threshold_dps,
            "demo": True,
        })


# ----------------------------------------------------------------------
# Choosing a transport
# ----------------------------------------------------------------------
#: Espressif's USB vendor ID. An ESP32-S3 running with "USB CDC On Boot"
#: enumerates as Espressif's own CDC device rather than through a CP210x or
#: CH340 bridge chip, so this is what identifies the board -- and its absence
#: from the port list is the single most useful symptom when nothing connects.
ESPRESSIF_VID = 0x303A


def find_board_port() -> Optional[str]:
    """The most likely serial port for the board, or None.

    Prefers a port whose USB vendor ID is Espressif's, which on a board with
    native USB CDC is exact rather than a guess.
    """
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    for port in ports:
        if port.vid == ESPRESSIF_VID:
            return port.device
    for port in ports:
        text = f"{port.description} {port.manufacturer or ''}".lower()
        if any(k in text for k in ("esp32", "espressif", "usb serial", "cdc")):
            return port.device
    return None


#: What the two note colours are called, in every spelling somebody might type.
#: The game's charts say "left" and "right"; the notes on screen are blue and
#: pink, and that is what a person looking at them will call them.
HAND_ALIASES = {
    "left": "left", "blue": "left", "l": "left",
    "right": "right", "pink": "right", "r": "right",
    "both": "", "any": "", "": "",
}


def normalise_hand(name: str) -> str:
    """Turn any spelling of a note colour into the chart's own word.

    Raises rather than guessing: a typo here silently sends every flick to the
    wrong colour, which looks like the board being broken.
    """
    key = str(name).strip().lower()
    if key not in HAND_ALIASES:
        raise ValueError(
            f"unknown hand {name!r} -- use left/blue, right/pink, or both")
    return HAND_ALIASES[key]


def find_all_board_ports() -> list[str]:
    """Every serial port that looks like one of these boards, best first."""
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    exact = [p.device for p in ports if p.vid == ESPRESSIF_VID]
    loose = [
        p.device for p in ports
        if p.vid != ESPRESSIF_VID
        and any(k in f"{p.description} {p.manufacturer or ''}".lower()
                for k in ("esp32", "espressif", "usb serial", "cdc"))
    ]
    return exact + loose


def make_link(port: str | None = None, host: str | None = None,
              baud: int = 921600, udp_port: int = 3333) -> tuple[Link, str]:
    """Build the link the caller asked for, and the target string for it."""
    if host:
        return UdpLink(port=udp_port), host
    target = port or find_board_port()
    if not target:
        raise RuntimeError(
            "No serial port found. Plug the board in, or pass --host to use "
            "WiFi. `python game_bridge.py --list` shows every port seen."
        )
    return SerialLink(baud=baud), target
