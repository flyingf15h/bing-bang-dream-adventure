# Usage

Everything the board and the dashboard can do, and what each thing is for.

For calibration specifically there is a companion page,
[CALIBRATION.md](CALIBRATION.md), which is the same text the dashboard shows
under *Calibrate → Show the step-by-step guide*.

**Contents**

1. [Getting set up](#1-getting-set-up)
2. [Connecting: USB or WiFi](#2-connecting-usb-or-wifi)
3. [The dashboard, tab by tab](#3-the-dashboard-tab-by-tab)
4. [Calibration](#4-calibration)
5. [Which way round is the board?](#5-which-way-round-is-the-board)
6. [Position and velocity](#6-position-and-velocity)
7. [Gestures: flicks, flips and quick movements](#7-gestures-flicks-flips-and-quick-movements)
8. [Firmware command reference](#8-firmware-command-reference)
9. [The wire protocol](#9-the-wire-protocol)
10. [Tests](#10-tests)
11. [Troubleshooting](#11-troubleshooting)
12. [What this cannot do](#12-what-this-cannot-do)

---

## 1. Getting set up

### Firmware

```bash
# One-time: the vendor library for the IMU
arduino-cli lib install ICM45605

arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

The QMC6309 driver is in the sketch folder; there is nothing to install for it.

### Dashboard

```bash
cd dashboard
pip install -r requirements.txt
python main.py                 # or: python main.py --port COM14
```

### First contact

Open any serial monitor at **921600 baud**. The board prints an inventory of
what it found, how it is configured and what its network state is, then a
readable sample block a few times a second. `help` lists every command.

Nothing needs configuring before the dashboard will work over USB.

---

## 2. Connecting: USB or WiFi

The **Link** selector at the top left picks the transport. Both carry exactly
the same protocol, and both accept commands, so nothing else in the dashboard
changes depending on which one you use.

### USB serial

Pick the port, press **Connect**. That is all.

The dashboard puts the board into CSV mode at 100 Hz on connect and restores
the human-readable mode when it disconnects, so a serial monitor opened
afterwards still shows something legible.

### WiFi (UDP)

Give the board credentials once, over USB:

```
wifi ssid My Network Name
wifi pass hunter2
wifi connect
```

The name and password may contain spaces — everything after the subcommand is
taken as the value. Both are stored in the ESP32's NVS and survive a power
cycle. The password is never echoed back, on either transport.

`wifi connect` prints the address it was given:

```
OK wifi connected, ip 192.168.1.50, -47 dBm, udp port 3333
```

Type that address into the dashboard's **Board address** box, leave the port
at 3333, and press **Connect**.

Add `wifi auto on` and the board will join that network by itself at every
boot, so the USB cable is only ever needed once.

**You do not have to tell the board where to send anything.** It streams to
whichever address last sent it a command, and connecting is itself a command,
so it redirects to you automatically. That also means a board left streaming
at a laptop that has gone away starts streaming to a new one as soon as that
one connects, with no reboot and no reconfiguration.

If you would rather pin it — for a logger that never sends anything, say —
`udp host 192.168.1.20 3333` fixes the destination until `udp auto` releases
it. The pinned address is stored in NVS too.

### Both at once

By default output goes to **both** transports simultaneously. You can watch
the board in a serial monitor while the dashboard drives it over WiFi, and
type commands into either. `sink serial`, `sink udp` and `sink both` control
this; `udp on` / `udp off` toggles just the network half and leaves serial
alone.

### What UDP costs you

Datagrams can be dropped or arrive out of order and nothing retransmits them.
That is the right trade for live telemetry — a sample that arrives late is
worth less than the one behind it — and the wrong one when a gap matters.
Every record carries the device timestamp, so loss appears as a gap in time
rather than as silently wrong data, and every calibration routine averages
over hundreds of samples and tolerates missing ones. If you want certainty
that a particular command arrived, send it over serial; the board acknowledges
either way.

Lines are packed into datagrams of up to 1200 bytes and flushed when the
buffer fills or the oldest line in it turns 10 ms old, so batching never costs
more than 10 ms of latency and a datagram never fragments.

---

## 3. The dashboard, tab by tab

The left half of the window is always visible: a 3D view of the board's
current attitude, and the attitude as numbers.

* **Solid slab** — the board. **Coloured arms** — its X / Y / Z axes.
* **Grey arm `g`** — measured gravity. **Amber arm `B`** — the magnetic field.
  With a good calibration both stay still while the board turns. If they wobble
  as you rotate it, the calibration is the thing to fix.
* **Attitude tiles** — roll, pitch, yaw, compass heading, the raw quaternion,
  and roll/pitch computed from gravity alone. That last one uses no filter at
  all, so it is the honest cross-check: with the board still, the fused values
  should agree with it.

### Live

Instantaneous readings, three rolling plots, and the die temperature and field
strength. The window length and whether calibration is applied are both
switchable — turning calibration off is the quickest way to see how much it is
actually doing.

### Motion

Position, path and gestures. [Section 6](#6-position-and-velocity) and
[section 7](#7-gestures-flicks-flips-and-quick-movements) cover it.

### Sensors

Everything configurable on either chip:

* Accelerometer ODR 12 Hz – 6.4 kHz and range ±2/4/8/16 g.
* Gyroscope ODR and range ±15.625 – ±2000 dps. The `15`, `31` and `62`
  entries are the datasheet's 15.625, 31.25 and 62.5 dps settings.
* The seven APEX motion algorithms, individually.
* **INT1 owner** — `stream`, `fifo` or `wom`. The interrupt pin can only serve
  one of them at a time, so these are mutually exclusive rather than
  combinable.
* The magnetometer's mode, ODR, range, both oversampling ratios and its
  set/reset driver, plus its built-in self-test.

This part has no high-FSR mode, so 32 g and 4000 dps do not exist and the
firmware rejects them rather than quietly clamping.

### Calibrate

Easy mode and the expert panel. [Section 4](#4-calibration).

### Events

Everything the board's APEX engine raised — taps, tilts, steps, free-fall,
low-g, high-g, raise-to-wake, wake-on-motion — newest first, plus the
host-side flick, flip and movement detections.

### Console

Every line the board sent, and a box to type commands into. `help` works here
exactly as it does in a serial monitor. Data records are hidden unless you ask
for them, because at 100 Hz they bury everything else.

---

## 4. Calibration

Three corrections, applied identically on the board and in the dashboard:

```
gyro_out  = gyro_raw - gyro_bias
accel_out = (accel_raw - accel_bias) * accel_scale
mag_out   = mag_soft @ (mag_raw - mag_bias)
```

Because the model is the same on both sides, a calibration computed in the
dashboard behaves identically once pushed to the board.

### Easy mode

**Calibrate → Easy mode** replaces the whole window with a guided screen that
gives one physical instruction at a time and no jargon at all. Six steps,
about four minutes:

1. **Put it down and let go.** Measures gyroscope bias. Capture only runs
   while the board is genuinely still, and a knock part-way through throws the
   partial away and says so.

   "Still" here means *the readings are not changing*, not that they read 1 g
   and 0 °/s. That distinction matters more than it sounds: easy mode is fed
   raw readings, because measuring the offsets is its whole job, and a
   perfectly healthy board can sit motionless on a desk reading 6 °/s and
   1.04 g. Judged against absolute values such a board never counts as still,
   so steps 1, 2 and 4 would wait for ever and the calibration that would have
   removed the offsets is the one thing that could never run. Judged on how
   much the readings vary, the offsets are irrelevant — which is the point.
   Loose sanity bands (within 0.25 g of 1 g, under 25 °/s) still reject a
   board being carried at a steady rate, which does look like rest to a spread
   test. The dead-reckoning ZUPT keeps the absolute test, because it is fed
   corrected readings and there the absolute question is the right one.
2. **Rest it on each of its six sides.** Measures accelerometer offset and
   gain. You never have to work out which face is up — it reads gravity and
   recognises each side by itself, ticking them off as you go.
3. **Wave it around.** Measures the magnetometer's hard- and soft-iron
   correction. The bar tracks how much of the sphere of directions you have
   covered, and the step ends by itself when the fit will succeed.
4. **Which way is which.** Names the board's axes for you. Which one is up it
   reads off gravity by itself; for the rest you shove the board across the
   table once. [Section 5](#5-which-way-round-is-the-board).
5. **Checking the result.** Four things that must be true of any correct
   calibration: a still board reports no rotation, gravity measures 1.00 g,
   Earth's field is 25–65 µT, and the board's axes have been named. Each
   failure names the step to redo.
6. **Save it.** *Save to the board* writes the correction into NVS, where it
   survives unplugging and applies with the dashboard closed. *Save to a
   file…* keeps a named copy as JSON wherever you like — one per board, say,
   or one to keep before trying a fresh calibration. The file button is on the
   last two steps and works whether or not the board is connected.

### Nothing is only in memory

A working copy lives at `~/.bbda/calibration.json`, written the moment any
step produces a result — in easy mode and in the expert panel alike — and read
back at startup. Closing the window, a crash, or the cable coming out half way
through easy mode costs nothing but the step in progress, and the next run
starts with what the last one measured. Nothing has to be pressed for this.

It is also the only copy that keeps the **mounting orientation**, which is
never pushed to the board: it describes how you use the board, not how it
behaves, and writing it there would turn the board's own printed readings away
from the axis names on the silkscreen.

*Load a saved file…*, on the first step of easy mode, puts a *different*
calibration back and jumps to step 5 rather than to the end. That is
deliberate: a file is a claim about a board, and the checks are the only thing
that can tell you it is still true of *this* board on *this* desk — a file
from a different board, or from a desk with different metal on it, will fail
them. With nothing connected there is nothing to check against, so it lands on
the save step instead.

### Expert panel

The same measurements with the numbers exposed, plus filter tuning:

* **Beta** — how hard the accelerometer and magnetometer pull the orientation
  estimate back. Low is smooth but slow to correct gyro drift; high tracks
  quickly and drags sensor noise in with it. 0.03–0.1 suits a board at rest or
  moving slowly.
* **Zeta** — gyroscope bias tracking. Leave it at zero once step 1 is done,
  or it just adds a second feedback loop to fight the first.

**Push to board** writes bias, scale and iron correction into the ESP32's NVS,
so the board's own output is corrected even with the dashboard closed.
**Save JSON** keeps a named host-side copy including the axis mapping, which
Push deliberately does not send. Neither is needed to keep your work — the
working copy described above is written whenever anything here measures
something, including **Read from board** and **Reset to identity**.

---

## 5. Which way round is the board?

The dashboard draws in a frame where **X is forward, Y is left and Z is up**.
Your board's own axes are whatever the silkscreen says, and the two only line
up by luck. The mapping between them is a display convention — it is applied
on the host and never pushed to the board, so the board's own printed readings
always stay in the axes marked on the PCB.

### Working it out by shoving the board

Easy mode's fourth step asks for almost nothing:

> Lay the board flat on the table the way you mean to use it. Shove it **away**
> from you and let it stop. (Optionally, shove it to your **right** as well.)

**Which way is up it works out on its own.** A board resting on a table reads
+1 g along whichever of its axes points at the sky, to a fraction of a degree,
with no skill involved. Asking someone to lift a board straight up measures
the same thing far worse — a hand tilts, and 5° of tilt leaks 0.09 g of
phantom horizontal acceleration into the reading, the same order as the whole
of a gentle lift. One free, exact measurement replaces two awkward, poor ones,
which is why the up and down movements are gone.

**The slide only has to pick a horizontal axis.** It is projected onto the
plane perpendicular to gravity — discarding exactly the component that tilting
corrupts — and then rotated about gravity into an estimate of "forward". `left`
follows as `up × forward`, and the three rows make the mapping directly.

Because the answer is one of only 24 axis mappings, and the result is snapped
to the nearest of them, **anything within 45° of the direction asked for lands
on the same answer.** So one roughly-aimed shove is enough and the second
slide only confirms it — the checklist ticks off after the first, and Next is
live from that moment. Measured against a simulated board, the step is correct
for:

| what you do | tolerated |
| --- | --- |
| how far, how fast | 3 cm over 0.4 s through to 20 cm over 1.8 s |
| aim | up to 40° off the direction asked for |
| rocking the board as you push | up to 20°, beyond which it declines rather than guessing |
| turning the board as you push | up to 35°, reported back as "push it, do not swivel it" |

Two things do the work behind that tolerance. Gravity is re-measured from a
run of matching still samples before *every* slide, rather than tracked
continuously — a slide across a level table barely changes the accelerometer's
*magnitude*, so a stationary detector cannot see one, and anything that averages
continuously will fold the beginning of the push into the baseline that push is
measured against. And during the slide, gravity is carried along with the
gyroscope rather than held fixed, which cancels the tilt leakage that would
otherwise dominate every other error in the step.

What is left over is honest about itself: how far the fit had to snap is
reported, and if two slides disagree the step says which one is the odd one
out instead of averaging them into a direction neither supports.

### By hand

The expert panel offers the same 24 mappings in a dropdown, with a live
readout of which face is currently pointing up so you can check your work.
Only the 24 that are genuine rotations are offered; the other 24 signed
permutations are mirrors. [CALIBRATION.md](CALIBRATION.md) has the four checks
to work through.

---

## 6. Position and velocity

**Read this before reading the position.** Position from an IMU alone is dead
reckoning. Acceleration is integrated twice with no external reference, so
error grows with the *square* of time spent moving: a residual bias of 10 mg
is roughly 5 cm after one second and 5 m after ten. There is no GPS, no camera
and no wheel encoder here to bound it. Treat the output as relative motion
over the last few seconds. It is not a position fix and no amount of tuning
will make it one.

The Motion tab draws the path in 3D on a 10 cm grid with the attitude triad at
the tip, and as a top-down map of the world X-Y plane.

### Two estimators

**Kalman + ZUPT** (the default) is a 9-state error-state Kalman filter over
position, velocity and body-frame accelerometer bias — the standard
construction from the ZUPT-aided inertial navigation literature that
essentially every foot-mounted pedestrian navigation system uses. Four things
distinguish it:

* **Standing still is a measurement, not a clamp.** This is the big one. When
  the board is detected as stationary, "velocity is zero, to within this much
  noise" is fed in as an observation. Because the filter has been tracking the
  correlation between velocity error, position error and bias error, that one
  observation corrects all three: the position is pulled back along the
  direction the error is known to have accumulated, and the bias estimate
  improves. A plain velocity clamp throws exactly that information away.
* **The bias is a tracked state in body axes**, not a fixed-gain leak in world
  axes, so it keeps its meaning when the board changes attitude and the filter
  knows how well it currently knows it.
* **Trapezoidal integration.** Exact when acceleration varies linearly across
  a step, where the rectangular form leaves about `a·dt/2` behind on every one.
  On a symmetric push-and-stop the two are level; on an asymmetric one — a
  gentle push and a sharp stop — the rectangular error does not cancel. Worth
  saying plainly: this is the *least* significant of the four. Measured on a
  constant-jerk ramp the difference is 4 µm against 5 mm, but on a symmetric
  move at 200 Hz the two schemes land within a few microns of each other.
* **The drawn path is corrected backwards.** When a stop ends a moving
  segment, the correction the filter just applied is spread back over that
  segment's points as a linear ramp, so the trajectory agrees with the
  corrected endpoint instead of ending in a visible jump. It is a cheap
  stand-in for a proper backward smoother.

The **Position uncertainty (1σ)** tile is the filter's own covariance, so it
is a real uncertainty rather than a rule of thumb.

**Simple integrator** is the straightforward version: rectangular steps, a
hard velocity clamp when still, a fixed-gain bias leak and a continuous
velocity leak to bound a missed clamp. It is kept because it is easy to reason
about and to check the other one against. Its drift tile is a rule-of-thumb
estimate from 10 mg of assumed bias, not a measurement.

Switching estimators resets both, so the comparison starts from a clean slate
each time. The **velocity leak** slider applies only to the simple one and is
disabled for the other — leaking velocity toward zero at a fixed rate is a
crude stand-in for knowing how uncertain the velocity is, and a covariance is
that knowledge done properly.

**Zero-velocity updates** can be turned off in either. Doing so is
instructive: the position runs away within seconds, which is what the whole
apparatus exists to prevent.

---

## 7. Gestures: flicks, flips and quick movements

All three run on the host, on calibrated and mount-corrected values, which is
what makes them tunable live. They are independent of the board's own APEX
algorithms, which continue to report separately on the Events tab.

### Flick — a short, sharp rotation

A flick is an impulse: angular rate rises past a threshold, peaks, and falls
back within a few hundred milliseconds. The detector is a state machine rather
than a bare threshold so that it reports the *peak of the whole event*; a
plain threshold would fire on the leading edge, where the direction is still
ambiguous.

Two knobs and three ways of naming the direction:

* **Trigger rate** (default 150 dps) — how sharp a rotation counts.
* **Board axes (6)** — the original behaviour. Reports the nearest board axis
  and its sign. **Axis dominance** is the fraction of the peak rotation lying
  on the winning axis; the default floor of 0.8 sits deliberately above the
  0.707 an exact 45° diagonal scores, so an ambiguous flick is rejected rather
  than assigned an axis the detector cannot really tell apart.
* **N directions, degrees from the front** (the default, at six) — names the
  direction the flick *went*, as an angle clockwise from straight up: **0° up,
  90° right, 180° down, 270° left**, so six directions come out as 0, 60, 120,
  180, 240 and 300. Four, six, eight and twelve are offered; six 60° sectors is
  a good compromise between resolution and reliability.
* **N directions, plane and rotation axis** — the raw view: project the
  rotation vector onto a plane of two board axes and report the sector it fell
  in. This names what the board turned *about*.

The difference between the last two is 90°, and it matters. A gyroscope
measures the axis a rotation happened about, which is at right angles to where
the flick went: flick the front of the board upwards — a pitch — and the
measured rotation is about the left-right axis. Report that raw and the answer
comes out a quarter turn from the truth, with the pitch a player calls "up"
named after the sideways axis and the *roll*, which is not a direction at all,
named "up" and "down" instead. "Degrees from the front" resolves it by
reporting the velocity of the board's front under that rotation,
`v = ω × front`, which is where the flick actually sent it. **Up and down are
pitches, left and right are yaws, and a roll about the front is refused** — it
leaves the front pointing exactly where it was, so it has no direction to
report. Use this mode for anything a person or a game consumes; use the plane
modes for checking the sensor, where the unmassaged answer is the useful one.

### Which axis is the front

*Front is …* has to match the board, and nothing can guess it. Run [easy mode's
orientation step](#5-which-way-round-is-the-board) and `+X` is correct, because
that step rotates the board's axes into forward / left / up before any of this
sees them. Without it the axes are whatever the silkscreen says — and naming
the wrong one as the front is exactly what makes rolls read as up and down,
because the roll axis and the front axis have been swapped.

The check takes one gesture: flick the board upwards and look at the tile. 0°
means the front axis is right. Anything else, or nothing at all, means try
another entry in the list. Up, right, down and left are then the board's own,
whichever way you happen to be holding it.

In either sector mode the dominance floor becomes the requirement that the
rotation lay *in the chosen plane at all* — a flick about the plane's normal
has no meaningful direction within it — and a second test rejects anything
landing too near the boundary between two sectors. The narrower the sectors,
the more often that second test fires. That is the real cost of asking for
more directions, and it is the honest behaviour: a direction that close to the
line between two sectors was not really either of them.

### Quick movement — a short, sharp shove

The translation counterpart: sliding the board across a desk, lifting it,
swiping it left. Directions are named in words — *forward*, *left*, *back*,
*right*, and *up* / *down* for a lift or a drop — once easy mode has worked
out which way the board faces. The same 4 / 6 / 8 / 12 sector choice applies,
and a lift is reported as `up` rather than forced into a horizontal sector.

Each detection reports the direction, the peak speed, the distance covered,
the peak acceleration and the duration.

**Two design points worth knowing, because both look like details and are
not:**

*The direction comes from the peak of the integrated velocity, not from the
acceleration.* A deliberate move starts at rest and ends at rest, so its
acceleration is a push followed by an equal and opposite brake and integrates
to zero over the event. Reading the direction from the net acceleration would
give noise; reading it from the instantaneous peak would give whichever of the
two phases was sharper, so a hard stop would report the move as having gone
*backwards*. Velocity has no such problem: it rises during the push, peaks
mid-move and falls during the brake, and its peak points the way the board
actually travelled.

*The board has to be at rest before a move can start.* That is not a nicety.
Reading direction from integrated velocity is only meaningful if the
integration started from a known velocity, and rest is the only velocity the
detector can know. Without the requirement, a move beginning while the
previous one was still being suppressed gets picked up half way through,
during its braking phase, and reported as having gone the opposite way — a
failure that is silent and looks exactly like a correct answer.

The velocity used here is integrated only across the event, from zero, so
unlike the dead-reckoned position it accumulates no error between gestures.
That is why the direction of a swipe is reliable even though position over
minutes is not.

The **Move trigger** slider sets how hard a shove has to be, in m/s². Raise it
if ordinary handling registers; lower it if deliberate movements are missed.
Reported distance is a slight underestimate because the event only begins once
acceleration crosses the trigger, which is a little way into the push.

### Flip — a settled change of which face is up

Unlike the other two this is a state change, not an impulse: it only fires
once the board is still again, so turning the board over slowly registers just
as reliably as throwing it over.

---

## 8. Firmware command reference

Commands are accepted from serial and UDP alike, are case-insensitive, and
reply `OK`, `OK <text>` or `ERR <text>`.

### Output

| Command | Effect |
|---|---|
| `help`, `?` | the command list |
| `info` | full banner: devices, configuration, network, calibration |
| `mode pretty\|csv\|off` | human-readable blocks, machine CSV, or silence |
| `rate <hz>` | output rate, 1–500 |
| `csvcal on\|off` | apply the stored calibration to the CSV stream (raw by default, so a host can compute a calibration from it) |
| `sink serial\|udp\|both` | which transports carry the output |

### Sensors

| Command | Effect |
|---|---|
| `accel <odr> <fsr>` | ODR 1, 3, 6, 12, 25, 50, 100, 200, 400, 800, 1600, 3200, 6400 Hz; FSR 2, 4, 8, 16 g |
| `gyro <odr> <fsr>` | same ODRs; FSR 15, 31, 62, 125, 250, 500, 1000, 2000 dps |
| `run stream\|fifo\|wom` | who owns INT1: APEX events, the FIFO watermark, or wake-on-motion |
| `apex <feature> on\|off` | `tilt`, `ped`, `tap`, `r2w`, `freefall`, `lowg`, `highg`, or `all` |
| `ped reset` | zero the step counter |

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
| `udp auto` | send to whoever last sent a command (the default) |
| `udp port <n>` | local listen port, default 3333 |
| `udp status` | as `wifi status` |

### Diagnostics

| Command | Effect |
|---|---|
| `debug on\|off` | announce every vendor-library call during a mode change before running it, so a hang can be attributed |
| `apexprobe` | report the return code of each step the vendor's `startAPEX()` folds into one result |

---

## 9. The wire protocol

Line-oriented ASCII. The first character types the record, so the same stream
is readable by a person and trivially parsed by a machine.

```
D,<t_us>,ax,ay,az,gx,gy,gz,mx,my,mz,temp_c,mag_fresh
E,<t_us>,<event>,<key=value ...>
I,<key>,<value>
OK / OK <text> / ERR <text>
```

* **`D`** — one sample. Accelerometer in g, gyroscope in dps, magnetometer in
  µT, temperature in °C. `mag_fresh` is 1 when the magnetometer produced a new
  sample for this record and 0 when the previous value was repeated. Values
  are **raw** by default so a host can compute a calibration from them;
  `csvcal on` applies the board's stored calibration instead.
* **`E`** — an APEX or wake-on-motion event: `tilt`, `pedometer`, `tap`,
  `r2w`, `freefall`, `lowg`, `highg`, `wom`.
* **`I`** — a key/value pair, emitted by `info` and `cal show` in CSV mode.

Timestamps are a free-running 32-bit microsecond counter, so they wrap about
every 71 minutes; the dashboard unwraps them.

Anything else — banners, help text, the pretty-printed sample blocks — is
human-facing and a machine parser should ignore it.

Over UDP a datagram usually holds whole lines but is not guaranteed to, so a
reader must assemble lines across datagram boundaries. `mode pretty` is the
default at boot; the dashboard switches to `mode csv` on connect.

---

## 10. Tests

```bash
python dashboard/tests/test_math.py      # 23 checks
python dashboard/tests/test_motion.py    # 134 checks
```

Neither needs hardware or a display.

`test_math.py` covers the ellipsoid fit, the six-position solve and the
Madgwick filter — convergence, each Euler axis, compass heading, the 6-axis
fallback, pure gyro integration and the dropped-sample guard.

`test_motion.py` covers stationary detection; both position estimators
against a known push, including the exactness of the trapezoidal form under
constant jerk and the fact that the two agree on a symmetric move; the Kalman
filter learning a standing bias, pulling position back at a stop, and running
away when zero-velocity updates are disabled; sector quantisation including
boundaries, wrapping, the clockwise convention and rejected configurations;
the calibration file round trip; flick detection in axis mode, across 4, 6 and
8 sectors with the boundary and out-of-plane rejections, and at all six
bearings from the board's front with the twist rejection;
quick-move detection in every direction including lifts, the hard-stop case,
the rest requirement and the nudge and noise rejections; and the frame solve
against all 24 mountings, with noise, with a genuinely crooked board, and
against the inputs it must refuse.

---

## 11. Troubleshooting

**The board is not found on any serial port.** It enumerates as an ESP32-S3
Dev Module with USB CDC on boot; the `CDCOnBoot=cdc` build flag matters.

**`wifi connect` reports "network not found".** The ESP32-S3 has no 5 GHz
radio. A 2.4 GHz network is required, and hidden SSIDs will not be found.

**UDP connects but no data arrives.** Check `udp status` over serial. If
`udp.target` is pinned to an old address, `udp auto` releases it. If the
sinks line says `serial only`, `sink both` fixes it. Some networks isolate
wireless clients from each other, which blocks this entirely.

**APEX events stopped after changing run mode.** Known and documented: the
vendor's `startAPEX()` can only succeed once per IMU power-on. Switching to
`fifo` or `wom` and back to `stream` leaves the motion algorithms off until
the board is genuinely power-cycled — a CPU reset is not enough, because it
leaves the IMU powered and holding its state. The banner reports
`imu.apex.active` so you can see when this has happened, and sensor streaming
continues regardless.

**The 3D model turns the wrong way.** The axis mapping is wrong, not the
calibration. Run easy mode's fourth step, or pick the mapping by hand.

**Flicking up reports something else, and rolling the board reports up and
down.** The front axis does not match the board. *Front is …* on the Motion
tab picks it; easy mode's fourth step makes `+X` correct. See
[section 7](#7-gestures-flicks-flips-and-quick-movements).

**Everything froze but nothing says why.** It does now: a connected board that
sends nothing for two seconds raises a red banner across the top of the window,
and easy mode shows the same warning above its instructions. The usual cause is
the USB cable, and if the port has disappeared from the system the banner says
so by name. Nothing measured is lost — the calibration on disk is written after
each step, so reconnecting and carrying on is all that is needed.

**Position drifts even though the board is still.** Check the State tile says
*stationary*. If it says *moving* while the board is not, the gyroscope bias
calibration is stale — redo step 1 — or something is vibrating the table.

**Gestures fire during ordinary handling.** Raise the trigger rate for flicks
or the move trigger for movements. If gestures are being *missed* instead,
lower them, and check that you are pausing between movements: a quick move
must begin from rest.

---

## 12. What this cannot do

Stated so nothing here is taken for more than it is.

* **Position is not a position fix.** Section 6 says why, at length.
* **Accelerometer cross-axis misalignment, gyroscope scale-factor error and
  temperature drift are all uncorrected.** Measuring them needs a rate table
  and a thermal chamber. The six-position method also assumes gravity is
  exactly 1 g, which is good to about 0.3 % anywhere on Earth.
* **Nothing distinguishes a constant-velocity slide from rest.** That is a
  property of the physics, not of this code: an accelerometer moving at a
  constant velocity reads exactly what a stationary one reads. Requiring the
  gyroscope to agree before declaring the board still is the best available
  mitigation and is not a solution.
* **Two APEX features on the ICM-456xx family datasheet do not exist on this
  part.** Bring-to-see and activity/inactivity detection are compiled only for
  the B1/C1 device families; this is an A1. The same applies to the on-chip
  GAF fusion quaternion, which is why orientation is fused on the host.
* **No high-FSR mode**, so 32 g and 4000 dps are unavailable and are rejected
  rather than silently clamped.
* **Motion-dependent features are verified against synthetic data only.** The
  board could not be moved during bring-up, so APEX tap / tilt / pedometer /
  free-fall events, the host gesture detectors and dead-reckoned position are
  all proven by the test suite and unproven on real motion.
