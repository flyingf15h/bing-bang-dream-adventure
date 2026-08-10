# Project reference

Everything about this project in one file: hardware, firmware, host tools, the
game, the wire formats between them, and the limits of each.

**Contents**

1. [System overview](#1-system-overview)
2. [Repository layout](#2-repository-layout)
3. [Hardware](#3-hardware)
4. [Firmware](#4-firmware)
5. [Board wire protocol](#5-board-wire-protocol)
6. [Firmware command reference](#6-firmware-command-reference)
7. [Networking: WiFi and UDP](#7-networking-wifi-and-udp)
8. [Dashboard](#8-dashboard)
9. [Calibration](#9-calibration)
10. [Axis mapping](#10-axis-mapping)
11. [Position and velocity](#11-position-and-velocity)
12. [Gesture detection](#12-gesture-detection)
13. [Game bridge](#13-game-bridge)
14. [The game](#14-the-game)
15. [Chart format](#15-chart-format)
16. [Leaderboard](#16-leaderboard)
17. [Tests](#17-tests)
18. [Troubleshooting](#18-troubleshooting)
19. [Known limitations](#19-known-limitations)

---

## 1. System overview

A rhythm game played by flicking a handheld IMU board. Four components:

| Component | Language | Role |
|---|---|---|
| `firmware/` | Arduino C++ | Samples the IMU + magnetometer, streams ASCII records over USB CDC and UDP, accepts commands, stores calibration in NVS |
| `dashboard/bbda/` | Python / PySide6 | Sensor dashboard, calibration, filters, position estimation, gesture detection |
| `dashboard/game_bridge.py` | Python | Runs the flick detector against a board and posts flick events to the game over localhost UDP |
| `game/` | Godot 4.7 / GDScript | The rhythm game; consumes flicks, mouse, touch and keyboard |
| `leaderboard/` | Static HTML/JS | Reads the score file the game writes |

Data path when playing: **board → (USB serial or WiFi UDP) → bridge → localhost
UDP :3334 → Godot `ImuInput` → `TapInputBus` → gameplay scoring.**

The game never talks to the board directly. Godot has no serial API, so the
bridge is what makes "USB or WiFi" a runtime choice. Detection lives only in
the bridge, so the dashboard and the game agree on what a flick is.

---

## 2. Repository layout

```
firmware/bbda_imu/       ESP32-S3 Arduino sketch
  bbda_imu.ino             main firmware
  protocol.h               record-type constants, protocol documentation
  qmc6309.{h,cpp}          register-level magnetometer driver

dashboard/               Python host tools (Python 3.10+)
  main.py                  dashboard entry point
  game_bridge.py           board -> game bridge entry point
  flick_check.py           flick aim/accuracy measurement tool
  requirements.txt
  bbda/
    app.py                 main window, tabs, wiring
    link.py                serial + UDP transport, line protocol parser
    calibration.py         ellipsoid fit, six-position solve, storage
    fusion.py              Madgwick filter
    motion.py              stationary detection, position estimators, gestures
    gamebridge.py          bridge logic and the bridge -> game wire format
    wizard.py              easy-mode calibration screen
    guide.py               step-by-step guide text (also shown in-app)
    view3d.py, widgets.py, theme.py
  tests/
    test_math.py           filter and calibration maths
    test_motion.py         estimators, gestures, frame solve
    test_gamebridge.py     bridge wire format and lane mapping

game/                    Godot 4.7 project
  project.godot
  autoload/
    TapInputBus.gd         input hub; every source reports taps here
    ImuInput.gd            UDP listener for the bridge
    ImuSettings.gd         tuning, persisted to user://imu_settings.cfg
  scenes/
    Start.tscn             title screen (main scene)
    MapSelect.tscn         chart picker
    Gameplay.tscn          the game
    ImuTest.tscn           IMU link diagnostic screen
  scripts/                 one .gd per scene, plus ImuDebugPanel.gd
  charts/                  beatmap JSON
  assets/{images,audio,video}

leaderboard/             static page the game feeds
  index.html
  leaderboard_wordmark.png
  scores.json, scores.js   written by the game, generated

_unused/                 files kept only for review; safe to delete
```

---

## 3. Hardware

Custom PCB, ESP32-S3 with two I²C devices on one bus:

| Part | Address | Notes |
|---|---|---|
| TDK ICM-45605 | 0x68 | 6-axis IMU, INT1 on GPIO17 |
| QST QMC6309 | 0x7C | 3-axis magnetometer |

Bus: SDA GPIO8, SCL GPIO9, 400 kHz. USB CDC at 921600 baud.

The ICM-45605 is driven through TDK's `ICM45605` Arduino library so every APEX
algorithm is the vendor implementation. The QMC6309 has no vendor Arduino
library; `qmc6309.cpp` is a register-level driver.

This part is an A1 device. Bring-to-see, activity/inactivity detection and the
on-chip GAF fusion quaternion are compiled only for B1/C1 families and do not
exist here — which is why orientation is fused on the host. There is no
high-FSR mode, so 32 g and 4000 dps are rejected rather than clamped.

---

## 4. Firmware

### Build and flash

```bash
arduino-cli lib install ICM45605           # one time
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

`CDCOnBoot=cdc` matters: without it the board does not enumerate as a serial
port.

### Behaviour at boot

Prints an inventory of devices found, configuration and network state, then a
readable sample block a few times a second (`mode pretty`). `help` lists every
command. Nothing needs configuring before the dashboard will work over USB.

### Persistent state (NVS)

Survives power cycles and reflashing: WiFi SSID and password, auto-join flag,
pinned UDP host/port, and the full calibration (gyro bias, accel bias and
scale, hard- and soft-iron correction). The mounting/axis mapping is
deliberately **not** stored on the board — it is a host display convention.

### Rescue build

Building with `-DBBDA_SKIP_AUTOJOIN=1` produces firmware that ignores the
stored auto-join setting. See [section 7](#7-networking-wifi-and-udp).

---

## 5. Board wire protocol

Line-oriented ASCII, newline terminated. The first character types the record.
Identical bytes over both transports.

```
D,<t_us>,ax,ay,az,gx,gy,gz,mx,my,mz,temp_c,mag_fresh
E,<t_us>,<event>,<key=value ...>
I,<key>,<value>
OK  |  OK <text>  |  ERR <text>
```

* **`D`** — one sample. Accel in g, gyro in dps, mag in µT, temperature in °C.
  `mag_fresh` is 1 when the magnetometer produced a new sample for this record,
  0 when the previous value was repeated. Values are **raw** by default so a
  host can compute a calibration from them; `csvcal on` applies the board's
  stored calibration instead.
* **`E`** — an APEX or wake-on-motion event: `tilt`, `pedometer`, `tap`, `r2w`,
  `freefall`, `lowg`, `highg`, `wom`.
* **`I`** — key/value pair, emitted by `info` and `cal show` in CSV mode.

Timestamps are a free-running 32-bit microsecond counter and wrap about every
71 minutes; the host unwraps them.

Anything else (banners, help text, pretty-printed blocks) is human-facing and
should be ignored by a parser.

Over UDP, lines are packed into datagrams of up to 1200 bytes, flushed when the
buffer fills or the oldest line in it turns 10 ms old — so batching costs at
most 10 ms of latency and a datagram never fragments. A reader must still
assemble lines across datagram boundaries. Datagrams may be lost or reordered
and nothing retransmits them.

---

## 6. Firmware command reference

Accepted from serial and UDP alike, case-insensitive, reply `OK`, `OK <text>`
or `ERR <text>`.

### Output

| Command | Effect |
|---|---|
| `help`, `?` | command list |
| `info` | full banner: devices, configuration, network, calibration |
| `mode pretty\|csv\|off` | human-readable blocks, machine CSV, or silence |
| `rate <hz>` | output rate, 1–500 |
| `csvcal on\|off` | apply stored calibration to the CSV stream (raw by default) |
| `sink serial\|udp\|both` | which transports carry the output |

### Sensors

| Command | Effect |
|---|---|
| `accel <odr> <fsr>` | ODR 1, 3, 6, 12, 25, 50, 100, 200, 400, 800, 1600, 3200, 6400 Hz; FSR 2, 4, 8, 16 g |
| `gyro <odr> <fsr>` | same ODRs; FSR 15, 31, 62, 125, 250, 500, 1000, 2000 dps |
| `run stream\|fifo\|wom` | who owns INT1: APEX events, FIFO watermark, or wake-on-motion |
| `apex <feature> on\|off` | `tilt`, `ped`, `tap`, `r2w`, `freefall`, `lowg`, `highg`, `all` |
| `ped reset` | zero the step counter |

`15`, `31` and `62` dps are the datasheet's 15.625, 31.25 and 62.5.

### Magnetometer

| Command | Effect |
|---|---|
| `mag mode susp\|norm\|single\|cont` | measurement mode |
| `mag odr 1\|10\|50\|100\|200` | output data rate, Hz |
| `mag range 8\|16\|32` | full scale, Gauss |
| `mag osr1 1\|2\|4\|8` | bandwidth filter ratio |
| `mag osr2 1\|2\|4\|8\|16` | low-pass filter depth |
| `mag sr on\|setonly\|off` | set/reset driver mode |
| `mag selftest` | built-in self test, per-axis values with pass/fail |
| `mag reset` | soft reset and reapply defaults |
| `mag reg <addr> [value]` | read or write a raw register (hex or decimal) |

### Calibration

| Command | Effect |
|---|---|
| `cal show` | print the stored calibration |
| `cal gyro <x> <y> <z>` | gyroscope bias, dps |
| `cal abias <x> <y> <z>` | accelerometer bias, g |
| `cal ascale <x> <y> <z>` | accelerometer per-axis gain |
| `cal mbias <x> <y> <z>` | hard-iron offset, µT |
| `cal msoft <m00..m22>` | soft-iron matrix, 9 values, row major |
| `cal save` | write to NVS |
| `cal clear` | restore the identity calibration |

### Network

| Command | Effect |
|---|---|
| `wifi ssid <name>` | network name; everything after the subcommand, spaces included |
| `wifi pass <secret>` | password, stored in NVS, never echoed back |
| `wifi connect` | join, waiting up to 12 s, then report the address |
| `wifi disconnect` | drop the network and stop listening |
| `wifi auto on\|off` | join automatically at boot |
| `wifi status` | the network section of the banner |
| `wifi forget` | erase the credentials |
| `udp on\|off` | include UDP in the output; serial is controlled separately |
| `udp host <ip> [port]` | pin the destination |
| `udp auto` | send to whoever last sent a command (default) |
| `udp port <n>` | local listen port, default 3333 |
| `udp status` | as `wifi status` |

### Diagnostics

| Command | Effect |
|---|---|
| `debug on\|off` | announce every vendor-library call during a mode change before running it, so a hang can be attributed |
| `apexprobe` | report the return code of each step the vendor's `startAPEX()` folds into one result |

---

## 7. Networking: WiFi and UDP

### The radio is 2.4 GHz only

The ESP32-S3 has no 5 GHz radio, and hidden SSIDs are not found. A 5 GHz-only
SSID fails with `could not join`, which does not say why. Check on Windows:

```powershell
netsh wlan show networks mode=bssid
```

Channel 1–14 is 2.4 GHz; 36 and up is 5 GHz. The PC being on the 5 GHz half of
the same SSID is fine.

### Setup, once, over USB

```
wifi ssid My Network
wifi pass hunter2
wifi connect
```

SSID and password may contain spaces — everything after the subcommand is the
value. The password is never echoed on either transport. A successful join
prints:

```
OK wifi connected, ip 10.2.219.181, -50 dBm, udp port 3333
```

`wifi auto on` makes it automatic at every boot. Set it only once the board has
proven stable on WiFi (see brownout, below).

### Addressing

The board streams to **whichever host last sent it a command**, and connecting
is itself a command, so it redirects automatically with no reboot. Two
consequences that look like faults but are not:

* The dashboard and the bridge fight over the board — only one gets the stream.
  Opening the dashboard silently steals it from the bridge, which then sits at
  0 Hz with its link still open. Over USB the clash is honest: the second
  program cannot open the port.
* `udp.target none yet` in `wifi status` is normal before anything has spoken
  to the board.

`udp host <ip> [port]` pins the destination; `udp auto` releases it. Both are
stored in NVS. By default output goes to **both** transports at once, so a
serial monitor and the dashboard can watch the same board simultaneously.

### Cost

| | USB serial | WiFi |
|---|---|---|
| Loss | none | occasional dropped datagram |
| Latency | ~1 ms, steady | a few ms on a quiet network |
| On a busy network | unaffected | 69–319 ms ping jitter measured here |
| Setup | none | credentials, and an IP that may change |
| Power draw | low | high, in bursts |
| Board is | tethered | free |

A dropped datagram costs at most one flick and never produces a wrong one:
detection runs over a window of samples and every record carries the device
timestamp, so loss shows as a gap rather than as bad data. Push calibrations
over USB if certainty that they arrived matters. Firmware disables modem sleep
(`WiFi.setSleep(false)`), so observed jitter is network contention.

### Brownout

Bringing the radio up draws 350–500 mA in microsecond bursts. A supply that
cannot deliver it sags, the brownout detector fires, and the chip resets. This
happened on this board. Symptoms in the order they appear:

| Stage | Symptom |
|---|---|
| USB only | perfect — hours at 200 Hz |
| First UDP commands | works; short bursts are within budget |
| Sustained streaming | a few dozen samples, then it stops |
| `wifi auto on` set | reboots about once a second, for ever |

The last row is the trap: the reset lands straight back in the join, so the
board never stays up long enough to accept the command that would disable it.
Measured here: 44 port appearances in 90 seconds, 117 attempts to open it, none
long enough to get a command in.

The fix is electrical, in rough order of likelihood: a shorter or thicker USB
cable; a different USB port (rear desktop ports beat front ports and hubs); a
powered hub or an external supply; a few hundred µF of bulk capacitance across
the 3V3 rail close to the module.

Confirmation before changing anything: the board works flawlessly on USB with
the radio off and fails only when it transmits. A broken radio or bad
credentials fails to *associate*; it does not reset the chip.

**Recovering a boot-looping board.** The firmware records an attempt in NVS
before joining and clears it on success, so two boots that both died mid-join
means the third comes up with the radio off and says so. Credentials and
settings are untouched; `wifi connect` retries by hand, `wifi auto off` stops
it trying at boot.

If the loop is tighter than the guard catches, build the rescue hatch:

```bash
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" \
  --build-property "compiler.cpp.extra_flags=-DBBDA_SKIP_AUTOJOIN=1" \
  --output-dir /tmp/bbda_rescue firmware/bbda_imu
arduino-cli upload --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" \
  --input-dir /tmp/bbda_rescue -p COM5
```

Boot it, send `wifi auto off`, flash the normal build back. Flashing works even
in a tight reset loop because the ROM bootloader runs before the sketch.
Erasing NVS (`esptool erase_region 0x9000 0x5000`) also works and takes the
calibration with it.

---

## 8. Dashboard

```bash
cd dashboard
pip install -r requirements.txt
python main.py                       # or --port COM14, or --host 192.168.1.50
```

Requires PySide6, pyqtgraph, PyOpenGL, numpy, pyserial.

The **Link** selector picks the transport; both carry the same protocol and
both accept commands, so nothing else changes with the choice. On connect the
dashboard puts the board into CSV mode at 100 Hz and restores the
human-readable mode on disconnect.

The left half of the window is always visible: a 3D view of the board's
attitude and the attitude as numbers.

* Solid slab — the board. Coloured arms — its X/Y/Z axes.
* Grey arm `g` — measured gravity. Amber arm `B` — the magnetic field. With a
  good calibration both stay still while the board turns.
* Attitude tiles — roll, pitch, yaw, compass heading, the raw quaternion, and
  roll/pitch computed from gravity alone. The last uses no filter, so it is the
  cross-check: at rest the fused values should agree with it.

| Tab | Contents |
|---|---|
| **Live** | Instantaneous readings, three rolling plots, die temperature, field strength. Window length and calibration-applied are switchable. |
| **Motion** | Position, path and gestures. Sections [11](#11-position-and-velocity) and [12](#12-gesture-detection). |
| **Sensors** | Accel ODR/range, gyro ODR/range, the seven APEX algorithms individually, INT1 owner (`stream`/`fifo`/`wom`, mutually exclusive), and the magnetometer's mode, ODR, range, both oversampling ratios, set/reset driver and self-test. |
| **Calibrate** | Easy mode and the expert panel. Section [9](#9-calibration). |
| **Events** | Every APEX and wake-on-motion event, newest first, plus host-side flick, flip and movement detections. |
| **Console** | Every line the board sent, and a box to type commands. Data records hidden unless requested. |

---

## 9. Calibration

Three corrections, applied identically on the board and in the dashboard, so a
calibration computed on the host behaves the same once pushed:

```
gyro_out  = gyro_raw - gyro_bias
accel_out = (accel_raw - accel_bias) * accel_scale
mag_out   = mag_soft @ (mag_raw - mag_bias)
```

Do it on a wooden or plastic table, away from laptops, speakers, phones, motors
and steel furniture — a steel table leg corrupts the magnetometer fit in a way
that looks successful. Let the board warm up for a minute or two; both bias and
offset move while the die temperature is climbing.

### Easy mode

*Calibrate → Easy mode* replaces the window with a guided screen: one physical
instruction at a time, six steps, about four minutes.

1. **Put it down and let go.** Gyroscope bias. Capture runs only while the
   board is genuinely still; a knock throws the partial away and says so.

   "Still" means *the readings are not changing*, not that they read 1 g and
   0 °/s. Easy mode is fed raw readings — measuring the offsets is its job —
   and a healthy board can sit motionless reading 6 °/s and 1.04 g. Judged
   against absolute values, such a board never counts as still and the
   calibration that would fix it could never run. Loose sanity bands (within
   0.25 g of 1 g, under 25 °/s) still reject a board carried at a steady rate.
   The dead-reckoning ZUPT keeps the absolute test, because it is fed corrected
   readings.
2. **Rest it on each of its six sides.** Accelerometer offset and gain. It
   reads gravity and recognises each side by itself.
3. **Wave it around.** Hard- and soft-iron correction. The bar tracks sphere
   coverage; the step ends by itself when the fit will succeed.
4. **Which way is which.** Names the board's axes. Section
   [10](#10-axis-mapping).
5. **Checking the result.** Four things true of any correct calibration: a
   still board reports no rotation, gravity measures 1.00 g, Earth's field is
   25–65 µT, the axes have been named. Each failure names the step to redo.
6. **Save it.** *Save to the board* writes into NVS. *Save to a file…* keeps a
   named JSON copy; that button is on the last two steps and works whether or
   not the board is connected.

*Load a saved file…* on step 1 puts a **different** calibration back and jumps
to step 5, not to the end — a file is a claim about a board, and the checks are
the only thing that can say it is still true of this board on this desk. With
nothing connected there is nothing to check against, so it lands on the save
step instead.

### Expert panel

The same measurements with numbers exposed, plus filter tuning.

* **Beta** — how hard accelerometer and magnetometer pull the orientation
  estimate back. Low is smooth but slow to correct gyro drift; high tracks
  quickly and drags noise in. 0.03–0.1 suits a board at rest or moving slowly.
  Tune by comparing fused *Roll/Pitch* against the *Accel-only roll/pitch* tile.
* **Zeta** — gyro bias tracking. Leave at zero once step 1 is done, or it adds
  a second feedback loop fighting the first.

**Push to board** writes bias, scale and iron correction to NVS. **Save JSON**
keeps a host-side copy including the axis mapping, which Push deliberately does
not send.

### Doing it by hand

1. **Gyroscope bias, ~10 s.** Set the board down, do not touch it, press
   *Capture*. The average reading is the bias. A large peak-to-peak spread
   means the table was nudged; run it again.
2. **Accelerometer, six positions, ~2 min.** Each axis needs one reading
   pointing at the ceiling and one at the floor: those bracket ±1 g, the
   midpoint is the offset, half the span is the gain error. Rest each edge
   against something solid — held in the air, a few degrees of drift becomes a
   gain error you will never see again.
3. **Magnetometer, ~1 min.** *Start collecting*, rotate slowly through as many
   attitudes as you can reach: figure-of-eights, then tumble about each axis.
   The coverage bar counts octants visited; a fit from one axis of rotation is
   worthless. Press *Fit ellipsoid*. Orange cloud is raw, green is corrected. A
   good result is a green sphere centred on the white origin dot with residual
   under about 2 %.

### Storage

A working copy lives at `~/.bbda/calibration.json`, written the moment any step
produces a result — easy mode and expert panel alike, including *Read from
board* and *Reset to identity* — and read back at startup. A crash or a cable
coming out costs only the step in progress. Nothing has to be pressed.

It is also the only copy that keeps the **mounting orientation**, which is
never pushed to the board: it describes how you use the board, not how it
behaves, and writing it there would turn the board's own printed readings away
from the axis names on the silkscreen.

---

## 10. Axis mapping

The dashboard draws in a frame where **X is forward, Y is left, Z is up**. The
board's own axes are whatever the silkscreen says. The mapping between them is
a host-side display convention and is never pushed to the board.

### By sliding the board (easy mode, step 4)

> Lay the board flat the way you mean to use it. Shove it **away** from you and
> let it stop. (Optionally, shove it to your **right** as well.)

Which way is up is worked out on its own: a board on a table reads +1 g along
whichever axis points at the sky, to a fraction of a degree. Asking someone to
lift the board measures the same thing far worse — 5° of hand tilt leaks 0.09 g
of phantom horizontal acceleration, the same order as the whole of a gentle
lift.

The slide only has to pick a horizontal axis. It is projected onto the plane
perpendicular to gravity — discarding exactly the component tilt corrupts —
then rotated about gravity into an estimate of "forward". `left` follows as
`up × forward`.

The answer is one of only 24 axis mappings and the result is snapped to the
nearest, so **anything within 45° of the direction asked for lands on the same
answer**. One roughly-aimed shove is enough; the second slide only confirms it.
Measured against a simulated board:

| what you do | tolerated |
|---|---|
| how far, how fast | 3 cm over 0.4 s through to 20 cm over 1.8 s |
| aim | up to 40° off the direction asked for |
| rocking the board as you push | up to 20°, beyond which it declines rather than guessing |
| turning the board as you push | up to 35°, reported as "push it, do not swivel it" |

Two things do that work. Gravity is re-measured from a run of matching still
samples before *every* slide rather than tracked continuously — a level slide
barely changes the accelerometer's magnitude, so a stationary detector cannot
see one, and anything averaging continuously folds the start of the push into
the baseline it is measured against. And during the slide, gravity is carried
along with the gyroscope rather than held fixed, cancelling the tilt leakage
that would otherwise dominate. How far the fit had to snap is reported, and if
two slides disagree the step names the odd one out instead of averaging them.

### By hand

The expert panel offers the same 24 mappings in a dropdown with a live readout
of which face is up. Only genuine rotations are offered; the other 24 signed
permutations are mirrors, which flip handedness, reverse every gyroscope
reading, and make the model spin backwards while looking almost right at rest.

Four checks; after each, if the model does the opposite of the board, change
the mapping and start again:

1. **Lay the board flat, component side up.** The model lies flat with its Z
   arm at the sky; the live line reads *+Z points up*.
2. **Tilt the front edge down.** The model pitches nose-down by the same
   amount. Compare against the *Accel-only roll/pitch* tile.
3. **Roll the board right.** The model rolls right.
4. **Rotate clockwise seen from above.** Heading *increases* — 0° is magnetic
   north, 90° east.

Checks 1–3 use gravity only and work with the magnetometer uncalibrated. Only
check 4 depends on the magnetometer, so if heading is the only thing wrong, redo
the magnetometer fit rather than changing the mapping.

---

## 11. Position and velocity

Position from an IMU alone is dead reckoning. Acceleration is integrated twice
with no external reference, so error grows with the *square* of time spent
moving: a residual bias of 10 mg is roughly 5 cm after one second and 5 m after
ten. There is no GPS, camera or wheel encoder here to bound it. Treat the output
as relative motion over the last few seconds. It is not a position fix.

The Motion tab draws the path in 3D on a 10 cm grid with the attitude triad at
the tip, and as a top-down map of the world X-Y plane.

### Kalman + ZUPT (default)

A 9-state error-state Kalman filter over position, velocity and body-frame
accelerometer bias — the standard construction from the ZUPT-aided inertial
navigation literature. Four distinguishing properties:

* **Standing still is a measurement, not a clamp.** When the board is detected
  stationary, "velocity is zero, to within this much noise" is fed in as an
  observation. Because the filter tracks the correlation between velocity,
  position and bias error, that one observation corrects all three: position is
  pulled back along the direction the error is known to have accumulated, and
  the bias estimate improves. A plain velocity clamp discards that information.
* **Bias is a tracked state in body axes**, not a fixed-gain leak in world
  axes, so it keeps its meaning when attitude changes and the filter knows how
  well it currently knows it.
* **Trapezoidal integration**, exact when acceleration varies linearly across a
  step, where the rectangular form leaves about `a·dt/2` behind on every one.
  The least significant of the four: on a constant-jerk ramp the difference is
  4 µm against 5 mm, and on a symmetric move at 200 Hz the two schemes land
  within microns of each other. It matters on asymmetric moves — a gentle push
  and a sharp stop — where the rectangular error does not cancel.
* **The drawn path is corrected backwards.** When a stop ends a moving segment,
  the correction just applied is spread back over that segment's points as a
  linear ramp, so the trajectory agrees with the corrected endpoint instead of
  ending in a jump. A cheap stand-in for a backward smoother.

The **Position uncertainty (1σ)** tile is the filter's own covariance.

### Simple integrator

Rectangular steps, a hard velocity clamp when still, a fixed-gain bias leak and
a continuous velocity leak to bound a missed clamp. Kept because it is easy to
reason about and to check the other against. Its drift tile is a rule-of-thumb
estimate from 10 mg of assumed bias, not a measurement.

Switching estimators resets both. The **velocity leak** slider applies only to
the simple one. **Zero-velocity updates** can be turned off in either; the
position then runs away within seconds, which is what the apparatus exists to
prevent.

---

## 12. Gesture detection

All three run on the host, on calibrated and mount-corrected values, which is
what makes them tunable live. They are independent of the board's APEX
algorithms, which continue to report separately on the Events tab.

### Flick — a short, sharp rotation

Angular rate rises past a threshold, peaks, and falls back within a few hundred
milliseconds. The detector is a state machine rather than a bare threshold so
it reports the *peak of the whole event*; a plain threshold fires on the leading
edge, where the direction is still ambiguous.

* **Trigger rate** (default 150 dps) — how sharp a rotation counts.
* **Board axes (6)** — reports the nearest board axis and its sign. **Axis
  dominance** is the fraction of the peak rotation on the winning axis; the
  default floor of 0.8 sits above the 0.707 an exact 45° diagonal scores, so an
  ambiguous flick is rejected rather than assigned.
* **N directions, degrees from the front** (default, at six) — names the
  direction the flick *went*, as an angle clockwise from straight up: 0° up,
  90° right, 180° down, 270° left. Six directions are 0, 60, 120, 180, 240, 300.
  Four, six, eight and twelve are offered.
* **N directions, plane and rotation axis** — projects the rotation vector onto
  a plane of two board axes and reports the sector. This names what the board
  turned *about*.

The difference between the last two is 90° and it matters. A gyroscope measures
the axis a rotation happened about, at right angles to where the flick went:
flick the front upwards — a pitch — and the measured rotation is about the
left-right axis. Reported raw, the pitch a player calls "up" is named after the
sideways axis, and *roll* — not a direction at all — is named "up" and "down".
"Degrees from the front" resolves it by reporting the velocity of the board's
front under that rotation, `v = ω × front`. **Up and down are pitches, left and
right are yaws, and a roll about the front is refused** — it leaves the front
pointing where it was, so it has no direction to report.

Use "degrees from the front" for anything a person or the game consumes; use
the plane modes for checking the sensor.

**Which axis is the front.** *Front is …* has to match the board and nothing
can guess it. After easy mode's orientation step, `+X` is correct, because that
step rotates the board's axes into forward/left/up first. The check takes one
gesture: flick upwards and look at the tile — 0° means the front axis is right.

In either sector mode the dominance floor becomes the requirement that the
rotation lay *in the chosen plane at all*, and a second test rejects anything
too near a sector boundary. Narrower sectors fire that test more often; that is
the real cost of asking for more directions.

### Quick movement — a short, sharp shove

The translation counterpart: sliding across a desk, lifting, swiping left.
Directions are named in words — forward, left, back, right, and up/down for a
lift or a drop — once the board's mounting is known. The same 4/6/8/12 sector
choice applies. Each detection reports direction, peak speed, distance, peak
acceleration and duration.

*The direction comes from the peak of the integrated velocity, not from the
acceleration.* A deliberate move starts and ends at rest, so its acceleration is
a push followed by an equal brake and integrates to zero. Net acceleration gives
noise; instantaneous peak gives whichever phase was sharper, so a hard stop
reports the move as going *backwards*. Velocity rises during the push, peaks
mid-move and falls during the brake, and its peak points the way the board went.

*The board has to be at rest before a move can start.* Integrated velocity is
only meaningful if integration started from a known velocity, and rest is the
only one the detector can know. Without the requirement, a move beginning while
the previous one is still suppressed gets picked up during its braking phase and
reported as having gone the opposite way — silently, and looking correct.

The velocity used here is integrated only across the event, from zero, so unlike
dead-reckoned position it accumulates no error between gestures.

The **Move trigger** slider sets how hard a shove must be, in m/s². Reported
distance slightly underestimates, because the event begins only once
acceleration crosses the trigger.

### Flip — a settled change of which face is up

A state change, not an impulse: it fires only once the board is still again, so
turning the board over slowly registers as reliably as throwing it over.

---

## 13. Game bridge

```bash
cd dashboard
python game_bridge.py                       # find the board on USB
python game_bridge.py --port COM7           # a particular serial port
python game_bridge.py --host 192.168.1.50   # over WiFi
python game_bridge.py --list                # what serial ports exist
python game_bridge.py --demo                # no board: fake flicks
```

Two boards, one per note colour:

```bash
python game_bridge.py --board left=COM7 --board right=COM9
python game_bridge.py --board blue=COM7:+Y --board pink=COM9:-X
python game_bridge.py --two-boards          # find both, left is the first
```

Blue notes are the left hand, pink the right. A board given a hand may only hit
notes of that colour; notes marked `any` and the gold bonus notes are open to
either. One board with no hand named plays everything. Both boards run in one
process posting to the same game port, because the game has one input path and
one set of detection tuning.

`-v` prints a line per flick — the quickest way to tell whether a flick that did
not register was missed by the detector or lost between the bridge and the game.

### Wire format, bridge → game

One JSON object per datagram, UTF-8, localhost, default port 3334. Every record
carries `v` (format version, currently 1) and `type`.

```json
{"v":1,"type":"hello","transport":"serial","target":"COM7","sectors":6}
{"v":1,"type":"flick","seq":12,"t":91.42,"host_t":1712.3,"bearing":88.7,
 "sector":1,"strength":0.61,"peak_dps":464.0,"dominance":0.93,"duration_ms":92.0}
{"v":1,"type":"status","connected":true,"rate_hz":198.4,"samples":19840,"flicks":12}
{"v":1,"type":"motion","bearing":88.7,"dps":210.4,"swing":198.1,"threshold_dps":150.0}
{"v":1,"type":"refused","reason":"swing","bearing":91.2,"peak_dps":388.0,
 "duration_ms":104.0,"detail":"mostly a roll -- only 0.41 of the turn ..."}
{"v":1,"type":"bye"}
```

* `bearing` is degrees clockwise from straight up, the convention `FlickFrame`
  reports. It is sent as a continuous angle rather than only a sector index so
  the game's own sector layout — which a chart can override — decides which lane
  was hit, instead of the direction being quantised twice.
* `motion` records are the board's *current* rotation, not a completed gesture,
  sent at `BridgeConfig.motion_hz` so the game can draw an arrow that follows
  the board. `dps` is the whole rotation rate, `swing` the part that moved the
  board's front, `threshold_dps` the rate a flick starts at. They are advisory;
  ignoring them plays exactly the same.
* `refused` records say a movement was seen and deliberately not called a flick,
  and why. Silence is the worst answer to "I flicked and nothing happened",
  because it cannot be told apart from an unplugged board. Nothing is ever
  scored from one; `detail` is a sentence meant to be shown.

The game listens on localhost only. If the bridge is not running, the game plays
on mouse, touch and keyboard — an absent bridge is a normal state, not an error.
Command line: `--imu-port=3334` to listen elsewhere, `--no-imu` to not open a
socket at all.

### flick_check.py

```bash
python flick_check.py watch            # live dial that follows the board
python flick_check.py aim              # guided: flick each lane, get a table
python flick_check.py record run.jsonl # capture raw samples
python flick_check.py replay run.jsonl # re-run detection over a capture
```

`aim` separates the two kinds of wrong: a **constant offset** (every flick lands
the same number of degrees round from where it was aimed — a mounting angle or
wrong front axis, one number, already corrected by `bearing_offset_deg`), and
the **spread** left after taking that offset out, which is the real directional
accuracy and cannot be calibrated away. Reporting only raw error mixes the two.

`record`/`replay` exist so tuning is measured on identical data rather than
against a fresh set of hand-thrown flicks.

It reads the front axis from the game's own settings file
(`%APPDATA%/Godot/app_userdata/bing bing rhythm war/imu_settings.cfg`) rather
than defaulting to `+X`: measured against the wrong front axis the swing plane
is wrong, so bearings are not merely offset, they are scrambled differently per
direction.

---

## 14. The game

Godot 4.7, Forward+, Jolt physics, d3d12 on Windows. Main scene
`res://scenes/Start.tscn`. Run from the editor, or:

```bash
godot --path game                              # normal
godot --path game res://scenes/ImuTest.tscn    # IMU link diagnostic
```

### Input architecture

Nothing in gameplay reads mouse, touch or IMU directly. Every source calls
`TapInputBus.report_tap()`; gameplay listens to the `tap` signal.

`TapEvent` carries:

| Field | Meaning |
|---|---|
| `source` | which input produced it |
| `timestamp_ms` | when it was reported |
| `screen_position` | where a pointer was; zero for the IMU |
| `strength` | 0.0–1.0, e.g. impact force; 1.0 for mouse/touch |
| `direction_deg` | degrees counter-clockwise from screen right, or NAN. A pointer names a direction by *where it is*, so mouse and touch leave it unset; the IMU has no position and names the direction outright. Consumers prefer this when set. |
| `lag_ms` | how long before the event the input really happened. Zero for a pointer. Not zero for the IMU: a flick cannot be recognised until it is over, so the report trails the gesture by about half its duration. Scoring must subtract this or every flick reads late — by a varying amount, since the player chooses how long a flick lasts, which is why it cannot be a fixed latency setting. |
| `hand` | `"left"` (blue notes), `"right"` (pink), or `""` for either |

`TapInputBus.tap_judged(source, hit)` reports what gameplay did with a tap, for
the on-screen arrow and the debug panel, neither of which should read the note
list.

### Autoloads

| Autoload | Role |
|---|---|
| `TapInputBus` | the input hub above |
| `ImuInput` | UDP listener; emits `flick_received`, `link_changed`, `board_changed`, `motion_updated`, `flick_refused`. Link timeout 3 s — the bridge sends a status record every second. `board_changed` is separate from `link_changed` because the bridge can be running perfectly while the board is unplugged. |
| `ImuSettings` | tuning, saved to `user://imu_settings.cfg` on change, format version 4 |

`ImuSettings` holds three kinds of value, which behave differently:

* **Detection** (front axis, thresholds, swing and margin floors) belong to the
  bridge, because that is where the detector runs. This node remembers them,
  sends them, and displays what the bridge says it is actually running —
  `applied` is that echo.
* **Display** (the arrow, whether anything but a scoring hit gets colour) are
  the game's own and take effect immediately.
* **Assist** (`lane_tolerance_deg`, default 75, clamped 30–100; `timing_scale`,
  default 2.8, clamped 1–4) are also the game's own, because they are about
  scoring rather than detection.

### Scoring

| Item | Value |
|---|---|
| Score pool | 1 000 000, distributed by note weight |
| Note weight | 1.0, ×1.5 if bonus, ×2 if it is a hold |
| Perfect window | ±45 ms × `timing_scale` |
| Near window | ±110 ms × `timing_scale` (judged EARLY or LATE) |
| Slide grace | 120 ms |
| Grades | SS+ ≥98 %, SS ≥95 %, S ≥90 %, A ≥80 %, B ≥70 %, C ≥60 %, D ≥50 %, else FAIL |

A flick reaches every note within `lane_tolerance_deg` of where it went, on
windows widened by `timing_scale`, and takes the one that best balances timing
against aim at `AIM_COST_MS_PER_DEG = 1.5` ms per degree.

### Keyboard

| Key | Action |
|---|---|
| `A S D` / `J K L` | lanes 6 5 4 / 1 2 3 |
| `Space` | pause |
| `R` | restart |
| `F` | autoplay |
| `N` | note numbers |
| `I` | IMU arrow on/off |
| `O` | arrow shows only registered flicks |
| `[` `]` | audio offset ∓5 ms |
| `;` `'` | audio offset ∓25 ms |
| `,` `.` | speed ∓0.05× |
| `Esc` | back to title |

### Debug panel

`ImuDebugPanel.gd` is built in code, not as a scene, so any screen can add it
with `add_child(preload("res://scripts/ImuDebugPanel.gd").new())`. It is on the
title screen and in gameplay. Nothing in it is computed locally: every control
sends its change to the bridge and displays what the bridge reports back, so a
slider that moved but did not take effect will not look like it did. It also
runs the guided direction check (four square directions: up, right, down, left)
and exports/imports settings as JSON.

### ImuTest scene

Diagnostic screen: the same six lanes gameplay uses, lighting the one each flick
maps to, plus the numbers behind the decision. It listens to `TapInputBus`
rather than `ImuInput`, so a flick that lights a lane here has travelled the
whole path — board, bridge, socket, bearing conversion, input bus. Only scoring
is left out.

---

## 15. Chart format

One JSON file per chart in `game/charts/`. Adding a chart is adding an entry to
`MAPS` in `scripts/MapSelect.gd` and dropping the JSON in — the gameplay scene
takes everything as exported properties. An empty audio or video path means the
chart genuinely has neither, which is treated differently from a file that
failed to load.

```json
{
  "title": "Bad Apple!!",
  "artist": "Masayoshi Minoshima feat. nomico",
  "audio_filename": "Bad-Apple-Cut-Audio.ogg",
  "bpm": 138.0,
  "travel_time_ms": 800,
  "hold_degrees_per_second": 200.0,
  "sector_angles": {"1":60.0,"2":0.0,"3":300.0,"4":240.0,"5":180.0,"6":120.0},
  "total_duration_ms": 46848,
  "note_count": 141,
  "notes": [
    {"time_ms":100,"sector":6,"hand":"left","type":"slide",
     "duration_ms":560,"sweep":1,"pitch_hz":312}
  ]
}
```

| Chart key | Default | Meaning |
|---|---|---|
| `travel_time_ms` | 800 | how long a note takes to reach the ring |
| `hold_degrees_per_second` | 200 | sweep rate for holds |
| `sector_angles` | 1:60, 2:0, 3:300, 4:240, 5:180, 6:120 | lane → screen angle, degrees |
| `total_duration_ms` | 0 | end of chart |

| Note key | Default | Meaning |
|---|---|---|
| `time_ms` | required | hit time |
| `sector` | 1 | lane |
| `hand` | `"left"` | `left`, `right` or `any` |
| `duration_ms` | 0 | non-zero makes it a hold |
| `sweep` | 1 | hold sweep direction |
| `bonus` | false | gold note, weight ×1.5, either hand |
| `pair_id` | −1 | groups notes that must be hit together |

---

## 16. Leaderboard

`leaderboard/index.html` is a static page — open it straight off disk or serve
the folder. Every finished run appends one record to `scores.json`:

```json
{"id":"1786261000-4821","song":"Bad Apple!!","score":988270,"grade":"SS",
 "pct":0.98827,"max_combo":162,"perfect":162,"early":0,"late":0,"miss":1,
 "at":"2026-08-09T14:22:31"}
```

The game writes two copies: `user://scores.json`, which always works including
in exported builds, and a public copy in the `leaderboard` folder next to
`index.html` so the page can fetch it as a sibling. In the editor that is
`../leaderboard` beside the Godot project; in an exported build it is a
`leaderboard` folder beside the executable, so shipping the page means copying
that one directory across.

The same data is also written as `scores.js` (`window.__scores`). A page opened
from `file://` cannot `fetch()` a sibling — file origins are opaque, so both
fetch and the File System Access API are unavailable — but a `<script>` tag can,
so the page re-injects `scores.js` on a timer and picks up new runs with no
action from the player. Over `http://` it polls `scores.json` directly. The
*Watch scores.json* button uses the File System Access API where available.

Grade cutoffs are duplicated in the page so it agrees with the game.

---

## 17. Tests

No hardware and no display required.

```bash
python dashboard/tests/test_math.py        # 23 checks
python dashboard/tests/test_motion.py      # 134 checks
python dashboard/tests/test_gamebridge.py
```

* **`test_math.py`** — ellipsoid fit, six-position solve, and the Madgwick
  filter: convergence, each Euler axis, compass heading, the 6-axis fallback,
  pure gyro integration, the dropped-sample guard.
* **`test_motion.py`** — stationary detection; both position estimators against
  a known push, including the exactness of the trapezoidal form under constant
  jerk and the agreement of the two on a symmetric move; the Kalman filter
  learning a standing bias, pulling position back at a stop, and running away
  with ZUPT disabled; sector quantisation including boundaries, wrapping, the
  clockwise convention and rejected configurations; the calibration file round
  trip; flick detection in axis mode, across 4/6/8 sectors with boundary and
  out-of-plane rejections, and at all six bearings from the front with twist
  rejection; quick-move detection in every direction including lifts, the
  hard-stop case, the rest requirement, nudge and noise rejections; and the
  frame solve against all 24 mountings, with noise, with a genuinely crooked
  board, and against inputs it must refuse.
* **`test_gamebridge.py`** — the bridge → game wire format and the lane mapping,
  restating `Gameplay.gd`'s `_nearest_sector()` so the two cannot drift.

---

## 18. Troubleshooting

**The board is not found on any serial port.** It enumerates as an ESP32-S3 Dev
Module with USB CDC on boot; the `CDCOnBoot=cdc` build flag matters.

**`wifi connect` reports "network not found".** No 5 GHz radio, and hidden SSIDs
are not found.

**UDP connects but no data arrives.** Check `udp status` over serial. A pinned
old address is released by `udp auto`; `serial only` in the sinks line is fixed
by `sink both`. Some networks isolate wireless clients from each other, which
blocks it entirely.

**Link opens, 0 Hz, no samples.** Something else took the stream — usually the
dashboard stealing it from the bridge. Close one of them.

**Reboots about once a second.** Brownout plus `wifi auto on`. Section
[7](#7-networking-wifi-and-udp).

**APEX events stopped after changing run mode.** The vendor's `startAPEX()` can
only succeed once per IMU power-on. Switching to `fifo` or `wom` and back to
`stream` leaves the motion algorithms off until a genuine power cycle — a CPU
reset is not enough, because it leaves the IMU powered and holding state. The
banner reports `imu.apex.active`. Sensor streaming continues regardless.

**The 3D model turns the wrong way.** The axis mapping is wrong, not the
calibration. Redo easy mode's step 4 or pick the mapping by hand.

**Flicking up reports something else, and rolling reports up and down.** The
front axis does not match the board. Set *Front is …* on the Motion tab; easy
mode's step 4 makes `+X` correct.

**Everything froze but nothing says why.** A connected board that sends nothing
for two seconds raises a red banner across the top of the window, and easy mode
shows the same warning. The usual cause is the USB cable; if the port has
disappeared, the banner says so by name. Nothing measured is lost.

**Position drifts even though the board is still.** Check the State tile says
*stationary*. If it says *moving* while the board is not, the gyro bias
calibration is stale or something is vibrating the table.

**Gestures fire during ordinary handling.** Raise the trigger rate or the move
trigger. If gestures are being missed, lower them, and pause between movements —
a quick move must begin from rest.

**A flick did not register in the game.** Run the bridge with `-v`. A line
printed there and nothing in the game means the datagram was lost or the port is
wrong; no line means the detector refused it, and the `refused` record says why.

---

## 19. Known limitations

* **Position is not a position fix.** Section [11](#11-position-and-velocity).
* **Accelerometer cross-axis misalignment, gyroscope scale-factor error and
  temperature drift are uncorrected.** Measuring them needs a rate table and a
  thermal chamber. The six-position method also assumes gravity is exactly 1 g,
  good to about 0.3 % anywhere on Earth.
* **Nothing distinguishes a constant-velocity slide from rest.** A property of
  the physics: an accelerometer moving at constant velocity reads exactly what a
  stationary one reads. Requiring the gyroscope to agree before declaring the
  board still is a mitigation, not a solution.
* **Two APEX features on the ICM-456xx datasheet do not exist on this part.**
  Bring-to-see and activity/inactivity are B1/C1 only; this is an A1. Same for
  the on-chip GAF fusion quaternion, which is why orientation is fused on the
  host.
* **No high-FSR mode**, so 32 g and 4000 dps are rejected rather than clamped.
* **Motion-dependent features are verified against synthetic data only.** The
  board could not be moved during bring-up, so APEX tap/tilt/pedometer/free-fall
  events, the host gesture detectors and dead-reckoned position are proven by
  the test suite and unproven on real motion.
* **WiFi on this board browns out under sustained transmit** unless the supply
  is improved. Section [7](#7-networking-wifi-and-udp).
</content>
</invoke>
