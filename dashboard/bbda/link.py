"""Transports and protocol parser.

Speaks the line protocol defined in firmware/bbda_imu/protocol.h. The parsing
is identical whatever carries the bytes, so it lives in :class:`Link` and the
two transports below only have to move bytes and say when they failed:

* :class:`SerialLink` -- USB CDC, the way the board arrives out of the box.
* :class:`UdpLink` -- WiFi, once the board has been given credentials.

Both own a reader thread; parsed records reach the GUI thread through Qt
signals, which are queued across the thread boundary automatically.

Choosing between them
---------------------
Serial is lossless, has no setup, and pins the board to a cable. UDP is
untethered and keeps up with the highest stream rates comfortably, but it is
datagram-based: packets can be dropped or arrive out of order, and nothing
retransmits them. That is the right trade for live telemetry -- a sample that
arrives late is worth less than the one behind it -- and the wrong one for
anything where a gap matters. Every record carries the device timestamp, so a
dropped packet shows up as a gap in time rather than as silently wrong data,
and the calibration routines all tolerate missing samples because they average
over hundreds of them. Push a calibration over serial if you want certainty
that the command arrived; the board acknowledges either way.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import serial
from serial.tools import list_ports
from PySide6.QtCore import QObject, Signal

DEFAULT_UDP_PORT = 3333


@dataclass
class Sample:
    """One synchronised reading, in engineering units."""

    t: float                 # device timestamp, seconds
    accel: np.ndarray        # g
    gyro: np.ndarray         # degrees / second
    mag: np.ndarray          # microtesla
    temp: float              # degrees C
    mag_fresh: bool          # False when the magnetometer value was repeated


@dataclass
class Event:
    """An APEX or wake-on-motion event."""

    t: float
    name: str
    detail: str
    fields: dict = field(default_factory=dict)


def available_ports() -> list[tuple[str, str]]:
    """Return (device, description) for every serial port on the system."""
    out = []
    for p in list_ports.comports():
        desc = p.description or ""
        out.append((p.device, f"{p.device}  -  {desc}"))
    return out


# ----------------------------------------------------------------------
# Shared protocol handling
# ----------------------------------------------------------------------
class Link(QObject):
    """Protocol parsing and the signals every transport emits.

    Subclasses implement :meth:`connect`, :meth:`disconnect` and
    :meth:`send`, and call :meth:`_feed` with whatever bytes arrive.
    """

    sample = Signal(object)       # Sample
    event = Signal(object)        # Event
    info = Signal(str, str)       # key, value
    line = Signal(str)            # every raw line, for the console
    status = Signal(str, bool)    # message, connected

    #: Shown in the UI so the console makes it obvious which one is in use.
    kind = "link"

    def __init__(self) -> None:
        super().__init__()
        self._buffer = bytearray()
        # Device timestamps are a free-running 32-bit microsecond counter, so
        # they wrap about every 71 minutes. These track the unwrap.
        self._last_raw_us = 0
        self._wraps = 0

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def connect(self, target: str) -> bool:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def send(self, command: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _reset_stream(self) -> None:
        self._buffer.clear()
        self._last_raw_us = 0
        self._wraps = 0

    def _start_stream(self) -> None:
        """Put the device into machine-readable mode at a useful rate."""
        self.send("mode csv")
        self.send("rate 100")

    def _feed(self, chunk: bytes) -> None:
        """Accept bytes from the transport and emit whole lines."""
        self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            raw, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                self._handle_line(text)

    def _timestamp(self, raw_us: int) -> float:
        """Unwrap the device's 32-bit microsecond counter into seconds."""
        if raw_us < self._last_raw_us - (1 << 31):
            self._wraps += 1
        self._last_raw_us = raw_us
        return (raw_us + self._wraps * (1 << 32)) / 1e6

    def _handle_line(self, text: str) -> None:
        self.line.emit(text)

        kind = text[0]
        if kind == "D":
            self._parse_data(text)
        elif kind == "E":
            self._parse_event(text)
        elif kind == "I":
            parts = text.split(",", 2)
            if len(parts) == 3:
                self.info.emit(parts[1], parts[2])

    def _parse_data(self, text: str) -> None:
        parts = text.split(",")
        # D + timestamp + 3 accel + 3 gyro + 3 mag + temp + freshness
        if len(parts) < 13:
            return
        try:
            t = self._timestamp(int(parts[1]))
            values = [float(v) for v in parts[2:12]]
            fresh = parts[12].strip() == "1"
        except ValueError:
            return

        self.sample.emit(
            Sample(
                t=t,
                accel=np.array(values[0:3], dtype=float),
                gyro=np.array(values[3:6], dtype=float),
                mag=np.array(values[6:9], dtype=float),
                temp=values[9],
                mag_fresh=fresh,
            )
        )

    def _parse_event(self, text: str) -> None:
        parts = text.split(",", 3)
        if len(parts) < 3:
            return
        try:
            t = self._timestamp(int(parts[1]))
        except ValueError:
            return
        name = parts[2]
        detail = parts[3] if len(parts) > 3 else ""

        fields: dict[str, str] = {}
        for token in detail.split():
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value

        self.event.emit(Event(t=t, name=name, detail=detail, fields=fields))


# ----------------------------------------------------------------------
# Serial
# ----------------------------------------------------------------------
class SerialLink(Link):
    """Owns the serial port and turns its output into typed records."""

    kind = "serial"

    def __init__(self, baud: int = 921600) -> None:
        super().__init__()
        self._baud = baud
        self._port: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._port is not None and self._port.is_open

    def connect(self, target: str) -> bool:
        self.disconnect()
        try:
            self._port = serial.Serial(target, self._baud, timeout=0.2)
        except serial.SerialException as exc:
            self.status.emit(f"Could not open {target}: {exc}", False)
            return False

        # An ESP32-S3 on native USB reboots when DTR/RTS toggle; give it a
        # moment and flush whatever the bootloader printed.
        time.sleep(0.3)
        self._port.reset_input_buffer()

        self._reset_stream()
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

        self.status.emit(f"Connected to {target}", True)
        self._start_stream()
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._port is not None:
            try:
                if self._port.is_open:
                    self.send("mode pretty")
                    self._port.close()
            except serial.SerialException:
                pass
            self._port = None
        self.status.emit("Disconnected", False)

    def send(self, command: str) -> None:
        """Send one command line. Safe to call from any thread."""
        if not self.connected:
            return
        payload = (command.rstrip("\n") + "\n").encode("ascii", errors="ignore")
        with self._write_lock:
            port = self._port
        if port is None:
            return
        try:
            with self._write_lock:
                port.write(payload)
                port.flush()
        except (serial.SerialException, OSError) as exc:
            self._fail(f"Lost the board while sending: {exc}")

    # ------------------------------------------------------------------
    def _fail(self, message: str) -> None:
        """Close the port after an error, from whichever thread noticed it.

        Emitting a status and leaving the port object in place -- which is what
        this used to do -- left :attr:`connected` reporting True after the cable
        was pulled. The button still said Disconnect, easy mode still believed
        it could capture, and every screen waiting on samples waited for ever
        with nothing on it to say why. Tearing the port down here is what makes
        the rest of the program's idea of "connected" true again.
        """
        self._stop.set()
        with self._write_lock:
            port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except (serial.SerialException, OSError):
                pass
        self.status.emit(message, False)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._port.read(4096)
            except (serial.SerialException, TypeError, AttributeError, OSError) as exc:
                # A deliberate disconnect closes the port under us, and the
                # error that causes is not news.
                if not self._stop.is_set():
                    self._fail(f"Lost the board: {exc}")
                return
            if chunk:
                self._feed(chunk)


# ----------------------------------------------------------------------
# UDP
# ----------------------------------------------------------------------
class UdpLink(Link):
    """Speaks the same protocol over WiFi.

    The board streams to whichever address last sent it a command, so nothing
    has to be configured on the board for this to work: opening the link sends
    a command, and that is what tells the board where to send. It also means a
    board that has been left streaming to a machine that went away starts
    streaming here instead as soon as this link opens, without a reboot.

    ``target`` is the board's address, either ``"192.168.1.50"`` or
    ``"192.168.1.50:3333"``. The board prints its address in its banner and in
    response to ``wifi status``.
    """

    kind = "udp"

    def __init__(self, port: int = DEFAULT_UDP_PORT) -> None:
        super().__init__()
        self._default_port = port
        self._socket: Optional[socket.socket] = None
        self._peer: Optional[tuple[str, int]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self.datagrams = 0

    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._socket is not None

    @staticmethod
    def parse_target(target: str, default_port: int) -> tuple[str, int]:
        text = target.strip()
        if ":" in text:
            host, _, port = text.rpartition(":")
            try:
                return host.strip(), int(port)
            except ValueError:
                return text, default_port
        return text, default_port

    def connect(self, target: str) -> bool:
        self.disconnect()
        host, port = self.parse_target(target, self._default_port)
        if not host:
            self.status.emit("Enter the board's IP address first", False)
            return False
        try:
            resolved = socket.gethostbyname(host)
        except OSError as exc:
            self.status.emit(f"Could not resolve {host}: {exc}", False)
            return False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Nothing is retransmitted, so a large receive buffer is what
            # stops a busy GUI thread from costing samples.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.settimeout(0.2)
            sock.bind(("", 0))
        except OSError as exc:
            self.status.emit(f"Could not open a UDP socket: {exc}", False)
            return False

        self._socket = sock
        self._peer = (resolved, port)
        self._reset_stream()
        self.datagrams = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

        self.status.emit(f"Streaming from {resolved}:{port}", True)
        # This first command is also what tells the board where to stream.
        self._start_stream()
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self.send("mode pretty")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._peer = None
        self.status.emit("Disconnected", False)

    def send(self, command: str) -> None:
        if self._socket is None or self._peer is None:
            return
        payload = (command.rstrip("\n") + "\n").encode("ascii", errors="ignore")
        with self._write_lock:
            try:
                self._socket.sendto(payload, self._peer)
            except OSError as exc:
                self.status.emit(f"Send failed: {exc}", False)

    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk, sender = self._socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.status.emit(f"Receive failed: {exc}", False)
                return
            if self._peer and sender[0] != self._peer[0]:
                continue      # something else on the network, not our board
            self.datagrams += 1
            # Each datagram holds whole lines, but a board that filled its
            # buffer mid-line will split one across two -- so the same
            # line-assembling buffer is used as for serial. A dropped packet
            # can therefore splice two half-lines together; the parser
            # rejects the result because the field count will not match.
            self._feed(chunk)
