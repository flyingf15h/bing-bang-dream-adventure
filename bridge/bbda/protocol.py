"""The bridge <-> game wire format, v2 -- one definition, read by both sides.

Every datagram is one JSON object, UTF-8, no trailing newline required, and
every one of them carries ``v`` (this format's version) and ``type``:

    {"v":2,"type":"hello","transport":"serial","target":"COM7","sectors":6,
     "rate_hz":400}
    {"v":2,"type":"flick","seq":12,"t":91.42,"host_t":1712.3,"bearing":88.7,
     "sector":1,"strength":0.61,"peak_dps":464.0,"dominance":0.93,
     "duration_ms":92.0}
    {"v":2,"type":"status","connected":true,"rate_hz":198.4,"samples":19840,
     "flicks":12}
    {"v":2,"type":"motion","bearing":88.7,"dps":210.4,"swing":198.1,
     "threshold_dps":150.0}
    {"v":2,"type":"refused","reason":"swing","bearing":91.2,"peak_dps":388.0,
     "duration_ms":104.0,"detail":"mostly a roll -- only 0.41 of the turn ..."}
    {"v":2,"type":"bye"}

``bearing`` is the direction the flick went in degrees clockwise from straight
up, which is the convention :class:`~bbda.motion.FlickFrame` reports and the
one a person describing a hand movement uses. The game converts it to its own
angle convention; see ``ImuInput.bearing_to_game_angle()``. It is sent as a
continuous angle rather than only as a sector index so that the game's own
sector layout -- which a chart can override -- stays the thing that decides
which lane was hit, instead of being quantised twice against a layout the
bridge guessed at.

``motion`` records are the board's *current* rotation rather than a completed
gesture, sent at :attr:`~bbda.service.BridgeConfig.motion_hz` so the game can
draw an arrow that follows the board in the hand. They exist because a flick
record arrives only after the flick is over and is refused outright when it
was too weak or too much of a roll -- so on the evidence of flicks alone, a
board that is being waved about and a board that is unplugged look identical.
They are advisory: a game that ignores them plays exactly as before.

``refused`` records say that a movement was seen and deliberately not called a
flick, and why. Silence is the worst possible answer to "I flicked and nothing
happened", because it cannot be told apart from a board that is unplugged.
Nothing is ever scored from one; ``detail`` is a sentence meant to be shown.

New this release (v2)
----------------------
``scan`` -- serial ports the bridge can see, and which look like boards, plus
any WiFi boards the bridge has heard a discovery beacon from recently. Sent
in reply to the ``scan`` command, so the game can offer a picker instead of
only "found" or "not found" -- and, for WiFi, instead of typing an IP in by
hand at all.

``cal_state`` -- calibration step, progress, prompt text and per-axis quality,
sent at ~10 Hz while a :class:`~bbda.calseq.CalSequence` is running, so the
game can draw a live quality bar rather than a spinner. The prompt text is
authored here, next to the thresholds that decide when a step passes, not in
GDScript -- splitting them would mean every threshold tweak needs an edit in
two languages.

``cal_done`` -- a calibration finished, accepted or rejected, with the
residuals that explain which.

``transport`` -- what a board is now reached on, after a switch requested by
the ``transport`` command. The existing ``hello`` already carries the
transport a board opened on; this is the same information after a live
change, without a bridge restart.

Commands (game -> bridge control port)
---------------------------------------
Existing: ``get``, ``set``, ``learn_front``, ``measure_rest``, ``write_bias``,
``reset``.

New this release: ``scan``, ``transport``, ``cal_start``, ``cal_advance``,
``cal_cancel``, ``cal_save``.
"""

from __future__ import annotations

#: Bump when a change would confuse an older ImuInput.gd; the game warns and
#: keeps going rather than failing hard. See ``game/autoload/wire.gd``, which
#: mirrors every name below -- if one side changes, so must the other.
WIRE_VERSION = 2

# ----------------------------------------------------------------------
# Record types: bridge -> game
# ----------------------------------------------------------------------
TYPE_HELLO = "hello"
TYPE_STATUS = "status"
TYPE_BYE = "bye"
TYPE_FLICK = "flick"
TYPE_MOTION = "motion"
TYPE_REFUSED = "refused"
TYPE_CONFIG = "config"
TYPE_FRONT_SUGGESTION = "front_suggestion"
TYPE_LEARNING = "learning"
TYPE_REST = "rest"
TYPE_MEASURING = "measuring"
TYPE_BIAS_WRITTEN = "bias_written"
TYPE_BOARD_CAL = "board_cal"
#: New in v2.
TYPE_SCAN = "scan"
TYPE_CAL_STATE = "cal_state"
TYPE_CAL_DONE = "cal_done"
TYPE_TRANSPORT = "transport"

# ----------------------------------------------------------------------
# Commands: game -> bridge control port
# ----------------------------------------------------------------------
CMD_GET = "get"
CMD_SET = "set"
CMD_LEARN_FRONT = "learn_front"
CMD_MEASURE_REST = "measure_rest"
CMD_WRITE_BIAS = "write_bias"
CMD_RESET = "reset"
#: New in v2.
CMD_SCAN = "scan"
CMD_TRANSPORT = "transport"
CMD_CAL_START = "cal_start"
CMD_CAL_ADVANCE = "cal_advance"
CMD_CAL_CANCEL = "cal_cancel"
CMD_CAL_SAVE = "cal_save"


def _present(**fields) -> dict:
    """``fields`` with anything left as ``None`` dropped.

    Several record shapes only ever set a subset of their optional fields per
    call, and the game tests for a key's presence rather than for ``null`` --
    a field sent as ``null`` would be read as a real, if odd, value rather
    than as absent. See ``refused``'s ``bearing`` for why that distinction
    matters: a flick that could not be judged against any frame must not read
    as one aimed at bearing zero.
    """
    return {key: value for key, value in fields.items() if value is not None}


def hello(transport: str, target: str, sectors: int, rate_hz: int) -> dict:
    """A board (or demo mode) has come up, and what it will be reached on."""
    return {"type": TYPE_HELLO, "transport": transport, "target": target,
            "sectors": sectors, "rate_hz": rate_hz}


def status(connected: bool, *, rate_hz: float | None = None,
           samples: int | None = None, flicks: int | None = None,
           detail: str | None = None, stalled: bool | None = None,
           gravity_ok: bool | None = None,
           wire_ms: float | None = None) -> dict:
    """Link and board health. Shapes vary by what changed; absent means

    unchanged from the last one sent -- this is not a full state dump every
    time, so a field's absence is not itself news. ``wire_ms`` is the worst
    transport delay seen since the last status, i.e. how stale a sample was
    by the time it got here -- the number that says whether WiFi feels
    laggy, rather than leaving that a matter of impression.
    """
    return {"type": TYPE_STATUS, "connected": connected,
            **_present(rate_hz=rate_hz, samples=samples, flicks=flicks,
                       detail=detail, stalled=stalled, gravity_ok=gravity_ok,
                       wire_ms=wire_ms)}


def bye() -> dict:
    """This bridge is shutting down."""
    return {"type": TYPE_BYE}


def flick(*, seq: int, t: float, peak_t: float, lag_ms: float, detect_ms: float,
          transport_ms: float, host_t: float, bearing: float, sector: int,
          strength: float, peak_dps: float, dominance: float,
          duration_ms: float, turn_deg: float, samples: int) -> dict:
    """One accepted flick, aimed and scored."""
    return {"type": TYPE_FLICK, "seq": seq, "t": t, "peak_t": peak_t,
            "lag_ms": lag_ms, "detect_ms": detect_ms,
            "transport_ms": transport_ms, "host_t": host_t, "bearing": bearing,
            "sector": sector, "strength": strength, "peak_dps": peak_dps,
            "dominance": dominance, "duration_ms": duration_ms,
            "turn_deg": turn_deg, "samples": samples}


def demo_flick(*, seq: int, t: float, bearing: float, strength: float,
               peak_dps: float, dominance: float, duration_ms: float) -> dict:
    """A made-up flick, for testing the game half with no board."""
    return {"type": TYPE_FLICK, "seq": seq, "t": t, "host_t": t, "peak_t": t,
            "lag_ms": 0.0, "bearing": bearing, "sector": -1,
            "strength": strength, "peak_dps": peak_dps, "dominance": dominance,
            "duration_ms": duration_ms, "demo": True}


def motion(*, bearing: float, dps: float, swing: float, threshold_dps: float,
           demo: bool = False) -> dict:
    """The board's current rotation, for the on-screen arrow to follow."""
    payload = {"type": TYPE_MOTION, "bearing": bearing, "dps": dps,
               "swing": swing, "threshold_dps": threshold_dps}
    if demo:
        payload["demo"] = True
    return payload


def refused(*, reason: str, peak_dps: float, duration_ms: float, detail: str,
            bearing: float | None = None) -> dict:
    """A movement that reached the threshold and was deliberately not scored."""
    return {"type": TYPE_REFUSED, "reason": reason, "peak_dps": peak_dps,
            "duration_ms": duration_ms, "detail": detail,
            **_present(bearing=bearing)}


def config(*, control_port: int, sectors: int, **tuning) -> dict:
    """What the bridge is actually running -- the reply to ``get`` and ``set``."""
    return {"type": TYPE_CONFIG, "control_port": control_port,
            "sectors": sectors, **tuning}


def learning(expect_bearing: float) -> dict:
    """Acknowledges ``learn_front``: the next real movement will be captured."""
    return {"type": TYPE_LEARNING, "expect_bearing": expect_bearing}


def front_suggestion(*, front: str, error_deg: float, swing: float,
                     expect_bearing: float, peak_dps: float, current: str,
                     candidates: list) -> dict:
    """Which front axis best explains the flick ``learn_front`` captured."""
    return {"type": TYPE_FRONT_SUGGESTION, "front": front,
            "error_deg": error_deg, "swing": swing,
            "expect_bearing": expect_bearing, "peak_dps": peak_dps,
            "current": current, "candidates": candidates}


def measuring(seconds: float) -> dict:
    """Acknowledges ``measure_rest``: the gyro bias measurement has started."""
    return {"type": TYPE_MEASURING, "seconds": seconds}


def rest(*, verdict: str, bias: list, bias_dps: float, peak_dps: float,
         samples: int) -> dict:
    """The result of ``measure_rest``: the gyro's bias while the board sat still."""
    return {"type": TYPE_REST, "verdict": verdict, "bias": bias,
            "bias_dps": bias_dps, "peak_dps": peak_dps, "samples": samples}


def bias_written(*, ok: bool, detail: str,
                 gyro_bias: list | None = None) -> dict:
    """The result of ``write_bias``."""
    return {"type": TYPE_BIAS_WRITTEN, "ok": ok, "detail": detail,
            **_present(gyro_bias=gyro_bias)}


def board_cal(gyro_bias: list) -> dict:
    """The board's own stored gyro bias, as it last reported it."""
    return {"type": TYPE_BOARD_CAL, "gyro_bias": gyro_bias}


def scan(ports: list, wifi: list | None = None) -> dict:
    """Serial ports the bridge can see, and WiFi boards it has heard from.

    ``ports`` is a list of ``{"device": ..., "description": ..., "looks_like_board":
    ...}``. ``wifi`` is a list of ``{"mac": ..., "ip": ..., "udp_port": ...}``,
    boards that announced themselves over the network within the last few
    seconds -- see ``bbda/service.py``'s ``BeaconListener``. Both are sent in
    reply to the ``scan`` command.
    """
    return {"type": TYPE_SCAN, "ports": ports, **_present(wifi=wifi)}


def cal_state(*, step: str, progress: float, prompt: str,
             quality: dict | None = None) -> dict:
    """A calibration sequence's current step, at ~10 Hz while it runs.

    ``step`` and ``prompt`` are authored on the bridge, next to the thresholds
    that decide when a step passes -- see ``bbda/calseq.py``. The game only
    ever displays them; it does not decide when a step is done.

    Carries no ``hand`` of its own: :meth:`GameBridge._emit` stamps that on
    every record this bridge sends, the same as ``flick`` or ``status``, and a
    second source for the same field is exactly the kind of thing that drifts.
    """
    return {"type": TYPE_CAL_STATE, "step": step,
            "progress": progress, "prompt": prompt,
            **_present(quality=quality)}


def cal_done(*, accepted: bool, detail: str,
             residuals: dict | None = None) -> dict:
    """A calibration sequence finished, accepted or rejected."""
    return {"type": TYPE_CAL_DONE, "accepted": accepted,
            "detail": detail, **_present(residuals=residuals)}


def transport_changed(*, transport: str, target: str) -> dict:
    """A board is now reached on a different transport, after a live switch."""
    return {"type": TYPE_TRANSPORT, "transport": transport, "target": target}
