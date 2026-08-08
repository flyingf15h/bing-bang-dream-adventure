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
    {"v":1,"type":"bye"}

``bearing`` is the direction the flick went in degrees clockwise from straight
up, which is the convention :class:`~bbda.motion.FlickFrame` reports and the
one a person describing a hand movement uses. The game converts it to its own
angle convention; see ImuInput.gd. It is sent as a continuous angle rather than
only as a sector index so that the game's own sector layout -- which a chart
can override -- stays the thing that decides which lane was hit, instead of
being quantised twice against a layout this process guessed at.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt

from .link import Link, SerialLink, UdpLink
from .motion import FlickDetector, flick_bearing_map, flick_frame

#: Where the game listens. One above the board's own 3333 so the two are
#: obviously related and cannot collide when board and game share a host.
DEFAULT_GAME_PORT = 3334

#: This is the ``v`` field above. Bump it when a change would confuse an older
#: ImuInput.gd; the game warns and keeps going rather than failing hard.
WIRE_VERSION = 1


@dataclass
class BridgeConfig:
    """Everything tunable, with the game's defaults already filled in."""

    # --- where the game is -------------------------------------------
    game_host: str = "127.0.0.1"
    game_port: int = DEFAULT_GAME_PORT

    # --- how the board is asked to stream ------------------------------
    rate_hz: int = 200
    calibrated: bool = True

    # --- what counts as a flick ----------------------------------------
    # 200 Hz rather than the dashboard's 100: a flick lasts 60-150 ms, and at
    # 100 Hz that is six samples to find a peak in. The peak is what names the
    # direction, so resolving it badly is not a small error -- it is a flick
    # reported as the wrong lane.
    on_threshold_dps: float = 150.0
    off_threshold_dps: float = 40.0
    # In bearing mode this is the swing floor: how much of the rotation moved
    # the board's front rather than twisting about it. 0.6 tolerates about 53
    # degrees of roll mixed into a flick, which a hand throwing one produces
    # easily; the detector's own 0.8 default is meant for a bench and rejects
    # too much to play with.
    min_swing: float = 0.6
    min_margin: float = 0.15
    # The return stroke is the reason this is not smaller. Flick the board up
    # and the hand brings it back down, and that return is a real rotation the
    # other way -- with no refractory period it lands as a second flick in the
    # opposite lane. Lower it for faster charts and expect phantom opposites.
    refractory_ms: float = 200.0
    min_duration_ms: float = 15.0
    max_duration_ms: float = 700.0

    # --- how the flick is described to the game ------------------------
    #: Sector count and offset the *detector* gates on. The game re-quantises
    #: the continuous bearing against its own layout, so these only decide
    #: which flicks are rejected as too close to a boundary. The default 30
    #: degree offset centres them on the game's lanes at 0/60/.../300.
    sectors: int = 6
    sector_offset_deg: float = 30.0
    front: str = "+X"
    #: Peak rate mapped to strength 1.0. A hard hand flick peaks near 700 dps.
    strength_ceiling_dps: float = 700.0

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
        self._rate_window: list[float] = []
        self._started_at = time.monotonic()
        self._last_status = 0.0
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
        )

    def attach(self, link: Link) -> None:
        """Take records from ``link`` from now on."""
        self._link = link
        link.sample.connect(self._on_sample, Qt.ConnectionType.DirectConnection)
        link.status.connect(self._on_status, Qt.ConnectionType.DirectConnection)

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

        # Link.connect() has already sent `mode csv` and `rate 100`; override
        # the rate, and ask the board to apply its own stored calibration so
        # the numbers here match what the dashboard shows.
        link.send(f"rate {self.config.rate_hz}")
        link.send("csvcal on" if self.config.calibrated else "csvcal off")

        self.link_up = True
        self.detector.reset()
        self.send_hello(link.kind, target)
        return True

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
        self._emit({
            "type": "hello",
            "transport": transport,
            "target": target,
            "sectors": self.config.sectors,
            "rate_hz": self.config.rate_hz,
        })

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

    def _on_sample(self, sample) -> None:
        now = time.monotonic()
        self.samples += 1

        rate = float((sample.gyro ** 2).sum() ** 0.5)
        self.peak_dps_seen = max(self.peak_dps_seen, rate)
        self.last_dps = rate

        self._rate_window.append(now)
        if self._rate_window and now - self._rate_window[0] > 1.0:
            cutoff = now - 1.0
            self._rate_window = [t for t in self._rate_window if t >= cutoff]

        # The detector is driven by the *device* clock. Host arrival times
        # carry USB and WiFi scheduling jitter, and a duration measured
        # through that jitter is what decides whether a flick is a flick.
        flick = self.detector.update(sample.t, sample.gyro, sample.accel)
        if flick is not None:
            self._publish(flick, now)

        self._maybe_send_status(now)

    def _publish(self, flick, host_now: float) -> None:
        self._seq += 1
        self.flicks += 1

        # In bearing mode the sector carries the measured angle it was derived
        # from, so this is the continuous bearing and not the sector centre.
        bearing = (
            flick.sector.angle_deg
            if flick.sector is not None
            else float(self.detector.frame.bearing_deg(flick.peak_vector))
        )

        # How long ago the flick actually peaked. A flick cannot be recognised
        # until it is over, so this record is always late -- by half the
        # gesture's duration, which the player varies freely. The game
        # subtracts this to judge the flick against when it happened rather
        # than when the news of it arrived; without it, a slow flick can never
        # score PERFECT and no fixed audio offset can correct for it, because
        # the error is not a constant.
        lag_ms = max(0.0, (float(flick.t) - float(flick.peak_t)) * 1000.0)

        self._emit({
            "type": "flick",
            "seq": self._seq,
            "t": round(float(flick.t), 4),
            "peak_t": round(float(flick.peak_t), 4),
            "lag_ms": round(lag_ms, 1),
            "host_t": round(host_now - self._started_at, 4),
            "bearing": round(float(bearing), 2),
            "sector": int(flick.sector.index) if flick.sector is not None else -1,
            "strength": round(self._strength(flick), 3),
            "peak_dps": round(float(flick.peak_dps), 1),
            "dominance": round(float(flick.dominance), 3),
            "duration_ms": round(float(flick.duration_ms), 1),
        })

        if self.config.verbose:
            print(
                f"[flick] #{self._seq:<4} bearing {bearing:6.1f}deg  "
                f"sector {flick.sector.index if flick.sector else '-'}  "
                f"{flick.peak_dps:6.0f} dps  {flick.duration_ms:5.1f} ms  "
                f"swing {flick.dominance:.2f}  lag {lag_ms:4.0f} ms"
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
