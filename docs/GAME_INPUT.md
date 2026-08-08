# Playing the game by flicking the board

The rhythm game takes six directions. Flick the board up-and-right and the
note in the upper-right lane is hit, the same as clicking there with the mouse
or pressing its key. This is how that works, how to set it up over either
transport, and what to do when a flick does not land.

---

## The path a flick takes

```
board                       host                                game
-----                       ----                                ----
ICM-45605 gyro
  |  200 Hz, D records
  |  USB CDC  or  UDP/WiFi
  v
                     game_bridge.py
                       FlickDetector  -> bearing, degrees clockwise from up
                       |  JSON datagram, UDP 127.0.0.1:3334
                       v
                                              ImuInput.gd
                                                bearing -> game angle
                                                v
                                              TapInputBus.report_direction()
                                                v
                                              node_2d.gd _on_tap -> _try_hit()
```

The last two steps are the same ones a mouse click goes through, so a flick is
scored by exactly the code that scores everything else. Nothing about timing
windows, combo or judgement is duplicated for the IMU.

### Why there is a bridge process

Godot has UDP built in and **no serial port support at all** — no engine API
for a COM port, and reaching one means shipping a GDExtension binary per
platform. So the game only ever speaks UDP on localhost, and the bridge is
what turns that into a choice of transport.

Detection runs in the bridge rather than in GDScript or on the board because
`FlickDetector` already exists, is tuned, and is what the dashboard draws. One
implementation means tuning it once and having the game and the dashboard
agree about what a flick is.

---

## Setting it up

Install the host dependencies once:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r dashboard/requirements.txt
```

Then start the bridge and leave it running next to the game.

**Over USB.** Nothing to configure; it finds the board by its USB vendor ID.

```bash
cd dashboard
python game_bridge.py
python game_bridge.py --port COM5      # if it picks the wrong one
```

**Over WiFi.** Give the board credentials once over USB, then point the bridge
at the address it prints:

```
wifi ssid <name>
wifi pass <password>
wifi connect
wifi auto on          # join at boot from now on
wifi status           # prints the IP
```

```bash
python game_bridge.py --host 192.168.1.50
```

Serial is lossless and needs no network, but pins the board to a cable — which
for a game played by flinging the board about is the wrong shape, though it is
the transport to debug with. WiFi frees the board for a few milliseconds of
jitter and the occasional dropped datagram, which costs at most one flick and
never produces a wrong one.

---

## Playing the whole game with only the board

The board is enough on its own — no reaching for the mouse between songs.

| Screen | Flick | Does |
|---|---|---|
| Title | any direction | start |
| Map select | any direction | confirm the map |
| Playing | one of six lanes | hit that lane |
| Results | **up** | replay |
| Results | **down** | back to the title |

Menus take any direction because there is nothing to choose between yet; the
results screen insists on up or down because it appears the instant the last
note resolves, and a player still mid-gesture would otherwise restart the song
by accident. Up and down there mean 90-degree wedges around straight up and
straight down, not lanes — `TapEvent.vertical()` decides.

The title screen says whether the bridge is running:

```
IMU ready — flick the board to start
no IMU bridge — run  python dashboard/game_bridge.py
```

Without that line, a title screen that ignores flicks looks broken rather than
unplugged. Mouse and keyboard keep working throughout; none of this replaces
them.

---

## Checking it works, in the order that isolates faults

Each step tests one more link in the chain, so the first one that fails tells
you where the fault is.

**1. Is the game listening?** Start the game and look for

```
[imu] listening on 127.0.0.1:3334
```

If instead it says `could not bind UDP 3334`, something else already has the
port — usually a second copy of the game. `--imu-port=3335` moves it, and the
bridge takes `--game-port 3335` to match.

**2. Does the game react to a fake flick?** With no board at all:

```bash
python game_bridge.py --demo
```

and run the diagnostic screen, which lights the lane each flick maps to:

```bash
godot --path . res://ImuTest.tscn
```

Six lanes should light in turn. If they do, everything from the socket
onwards is fine and the fault is upstream — board, cable, WiFi, or tuning. If
they do not, it is the port number or the game's own wiring.

**3. Is the board streaming?**

```bash
python game_bridge.py --port COM5
```

should settle at roughly the rate you asked for:

```
[link] 207 Hz  1957 samples  0 flicks
```

**4. Are your flicks strong enough?**

```bash
python game_bridge.py --port COM5 --monitor
```

prints the peak rotation rate twice a second against the threshold. A board
sitting still reads well under 1 dps; a deliberate flick should read several
hundred and say `over`. This is the one question the flick log cannot answer,
because a flick that was refused leaves no entry in it.

**5. Do flicks reach the game?** Run the bridge and `ImuTest.tscn` together.
The arrow shows the direction each flick came in at and the lit lane shows
where it was mapped.

---

## Timing: why a flick carries a lag with it

A flick cannot be recognised until it is over. The detector waits for the
rotation rate to fall back below its off threshold, because only then does it
know where the peak was — and the peak is what names the direction. So the
report always trails the gesture.

By exactly half of it, measured:

| flick duration | reported late by |
|---|---|
| 60 ms | 30 ms |
| 90 ms | 45 ms |
| 120 ms | 60 ms |
| 150 ms | 75 ms |

The game's PERFECT window is ±45 ms. Left uncorrected, a leisurely 120 ms
flick could never score PERFECT no matter how well timed it was — and, worse,
**the error is not a constant**: the player chooses how long a flick lasts, so
it moves from flick to flick and no fixed `audio_offset_ms` can remove it. The
input would feel arbitrary in a way that looks like bad detection.

So every flick carries `lag_ms`, the gap between its peak and its report.
`TapEvent.lag_ms` passes it through the input bus and `_try_hit()` backdates
the judgement by it, scoring the flick against when it happened rather than
when the news arrived. Mouse, touch and keys pass zero, since a click is known
the instant it occurs.

`audio_offset_ms` (the `[` `]` `;` `'` keys in game) is still the right knob
for *your* systematic offset — display lag, audio buffering, personal habit.
It is now correcting only things that really are constant.

**Known limitation.** Notes are culled `win_near` (110 ms) after their time,
measured in real time rather than backdated. A flick that was itself late by
more than `110 - lag_ms` finds the note already gone. In practice that only
affects hits that were heading for a MISS anyway, and it is much smaller than
the bug it replaced — but it does mean the late half of the window is slightly
tighter than the early half when playing with the board.

---

## When flicks are detected but land in the wrong lane

Almost always the board's **front axis** is wrong. The detector needs to know
which board axis points away from you, because a flick's direction is where
the front went — `omega x front` — and naming the wrong axis as the front
turns rolls into ups and downs.

```bash
python game_bridge.py --front +Y      # try each of +X -X +Y -Y +Z -Z
```

In `ImuTest.tscn` a wrong front shows up as an arrow that is consistently
rotated from the lane you aimed at, rather than as scatter.

If the arrow is right but the lane is off by half a lane, the sector grid is
misaligned: `--sector-offset` rotates it, and 30 degrees is the default
because the game's lanes sit at 0/60/…/300 counter-clockwise from screen
right, so a grid aligned to zero would put every flick on a boundary.

**There is no lane at the top of the ring.** The lanes are at 0/60/…/300, so
90 — straight up — falls exactly between two of them. A flick straight up is
genuinely ambiguous and is refused rather than guessed at. This is correct
behaviour and surprises everyone once.

---

## When flicks are not detected

| Symptom | Cause | Fix |
|---|---|---|
| `--monitor` says `under` | not a sharp enough movement | flick from the wrist, or `--threshold 100` |
| detected on the bench, refused in play | too much roll mixed in | `--swing 0.45` (0.6 default) |
| some directions work, others never | flick landing on a lane boundary | `--margin 0.05`, or check `--front` |
| a second flick fires the opposite way | the return stroke | raise `--refractory`; 200 ms is the default |
| fast charts drop inputs | refractory too long | `--refractory 120`, and expect phantom opposites |

The return-stroke trade is the awkward one and cannot be tuned away: flick the
board up and your hand brings it back down, and that return is a real rotation
in the opposite direction. The refractory period is the only thing separating
them.

---

## ESP32-S3 with USB CDC on boot

This board has **no USB-to-serial bridge chip**. The USB port is driven by the
ESP32-S3 itself, and the COM port is provided by the running sketch. Several
consequences are worth knowing before they waste an afternoon.

**The port only exists while the firmware runs.** No port in `--list` means
the sketch is not running, not that the cable is bad — though a charge-only
cable produces the same symptom. Re-flash holding BOOT if the firmware has
been bricked.

**The port disappears on reset and can come back under a different number.**
This is normal. The bridge reconnects by re-running its search rather than
reopening the old name, so a power-cycle mid-game recovers on its own.

**Baud rate is meaningless.** 921600 is what the code says, but a native CDC
endpoint ignores it — throughput is the USB link's, not a UART's. Setting a
different baud in a serial monitor changes nothing.

**Writes block when nothing is reading.** This is the failure that looks like
a crash and is not. A CDC `write()` waits for the host to drain the buffer,
100 ms by default, and at 200 Hz every sample is a write. If the host stops
reading — the bridge exits, the game is alt-tabbed, a serial monitor is left
scrolled back, the cable is in a sleeping laptop — `loop()` stalls, the FIFO
stops being serviced and WiFi is starved. The board looks dead while being
perfectly healthy, and over WiFi the stutter has no visible connection to USB
at all.

The firmware sets `Serial.setTxTimeoutMs(0)` in `setup()` to prevent this,
which makes a write drop what it cannot deliver instead of waiting. That is
the right trade for telemetry: the newest sample is worth more than the one
being waited on, records are written whole so nothing is corrupted, and every
record carries a timestamp so a loss reads as a gap.

**Opening the port can reboot the board.** This one is worth knowing about
because every symptom it produces points somewhere else.

pyserial raises DTR and RTS when it opens a port. On a board with a USB-to-
serial bridge chip those lines are wired to EN and IO0 and the behaviour is
well known — but this board has no bridge chip, and the ESP32-S3's own
USB-Serial-JTAG peripheral watches the same two lines for the reset sequence
esptool uses. So merely *connecting* resets it: the USB device re-enumerates,
and the handle just returned refers to something that no longer exists.

Measured on this board, five opens each way:

| Open | Clean | Rebooted | Failed |
|---|---|---|---|
| pyserial defaults | 2/5 | 1 | 2 |
| DTR/RTS held low | **5/5** | 0 | 0 |

It presents as `could not open port` / `FileNotFoundError`, or
`ClearCommError failed (PermissionError 13)` partway through a session, or a
write failing with "the device does not recognize the command" — all of which
look like a flaky cable and are not.

`SerialLink` now configures both lines low on an unopened port and then opens
it, so they are never raised at all. Assigning them *after* opening would not
help: the toggle is the edge the board resets on.

This is safe because the firmware is built for hardware CDC, where output
flows regardless of DTR. A TinyUSB CDC build gates output on DTR and would go
silent instead — if the USB mode is ever changed, this needs revisiting.

**Only one program can hold the port.** The dashboard and the bridge cannot
both open COM5. Over WiFi they conflict differently and more confusingly: the
board streams to whichever host spoke to it last, so opening the dashboard
silently steals the stream from the bridge. Symptom: the bridge sits at 0 Hz
with the link still open, which is what its "open but no samples" message is
telling you about.

**`mode pretty` on disconnect.** Both the dashboard and the bridge put the
board back into human-readable mode when they exit cleanly. If a bridge is
started while the board is in `pretty` mode it sends `mode csv` itself, so
this only bites if something exits uncleanly and is fixed by reconnecting.

---

## Tuning reference

Everything is a flag on `game_bridge.py`; `--help` lists them all.

| Flag | Default | What it does |
|---|---|---|
| `--front` | `+X` | board axis pointing away from the player |
| `--threshold` | 150 | dps that starts a flick |
| `--swing` | 0.6 | how much of the rotation must be a swing, not a roll |
| `--margin` | 0.15 | how far from a lane boundary a flick must land |
| `--refractory` | 200 | ms ignored after a flick |
| `--sectors` | 6 | lanes |
| `--sector-offset` | 30 | degrees the sector grid is rotated |
| `--rate` | 200 | samples per second asked of the board |
| `--game-port` | 3334 | must match the game's `--imu-port=` |

Game-side flags: `--no-imu` disables the listener entirely, `--imu-port=N`
moves it.

---

## Wire format

One JSON object per datagram, UTF-8, to `127.0.0.1:3334`.

```json
{"v":1,"type":"hello","transport":"serial","target":"COM5","sectors":6,"rate_hz":200}
{"v":1,"type":"flick","seq":12,"t":91.42,"peak_t":91.37,"lag_ms":46.0,
 "host_t":1712.3,"bearing":88.7,"sector":1,"strength":0.61,"peak_dps":464.0,
 "dominance":0.93,"duration_ms":92.0}
{"v":1,"type":"status","connected":true,"rate_hz":198.4,"samples":19840,"flicks":12}
{"v":1,"type":"bye"}
```

`bearing` is degrees clockwise from straight up — 0 up, 90 right, 180 down —
which is how a person describes a movement they made with their hand. The game
converts to its own convention with `90 - bearing`, since it measures
counter-clockwise from screen right.

The bearing is sent as a continuous angle and not only as a sector index, so
the game's own lane layout stays the thing that decides which lane was hit.
A chart can override that layout, and the bridge does not know it.

`dashboard/tests/test_gamebridge.py` restates the conversion and checks it
against the real lane layout, because a mistake there does not throw — it
just puts every flick in the wrong lane.
