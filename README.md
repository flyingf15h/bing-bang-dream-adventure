# bing-bang-dream-adventure

Firmware and a desktop dashboard for a custom ESP32-S3 board carrying a
**TDK InvenSense ICM-45605** 6-axis IMU and a **QST QMC6309** 3-axis
magnetometer.

The firmware exposes every feature both parts implement over a readable line
protocol, carried over USB serial and UDP at the same time. The dashboard
consumes that stream, fuses it into a live 3D orientation, and calibrates and
tunes the IMU.

**[docs/USAGE.md](docs/USAGE.md) documents everything in full.** This page is
the overview and the record of what was measured on real hardware.

---

## Hardware

| Signal | Net | Notes |
|---|---|---|
| I²C SDA | `GPIO8` | shared by both devices |
| I²C SCL | `GPIO9` | 400 kHz |
| ICM-45605 `INT1` | `GPIO17` | push-pull, active high, pulsed |
| ICM-45605 address | `0x68` | `AP_AD0` low |
| QMC6309 address | `0x7C` | fixed, the part has only one address |

> **Assumption worth checking:** the QMC6309 is wired to the same host I²C bus
> as the IMU. If it is instead on the ICM-45605's *auxiliary* I²C master pins,
> the magnetometer must be reached through the IMU's pass-through or I²C-master
> mode rather than `Wire` — say so and it is a small change confined to
> `readMag()` in `bbda_imu.ino`.

## Layout

```
firmware/bbda_imu/     Arduino sketch, QMC6309 driver, protocol definition
dashboard/             PySide6 + pyqtgraph dashboard
dashboard/game_bridge.py   feeds IMU flicks to the game, over USB or WiFi
dashboard/tests/       self-checks for the maths (no hardware needed)
ImuInput.gd            game-side UDP listener for those flicks (autoload)
ImuTest.tscn           diagnostic screen for the IMU link
docs/USAGE.md          everything the board and dashboard can do
docs/CALIBRATION.md    calibration + axis-alignment guide (also shown in-app)
docs/GAME_INPUT.md     playing by flicking the board, and fixing it when it stops
docs/WIFI.md           running the board untethered, and the brownout that stops it
```

---

## Quick start

**Firmware**

```bash
# One-time: install the vendor library for the IMU
arduino-cli lib install ICM45605

# The board enumerates as an ESP32-S3 Dev Module with USB CDC on boot
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

Open a serial monitor at **921600** baud — though on this board that number is
decoration: the USB port is the ESP32-S3's own, so there is no UART whose rate
could be set. The board prints a full inventory of what it found and how it is
configured, then a readable sample block a few times a second. Type `help` for
the command list.

**Playing the game with the board**

Start the bridge next to the game and flick the board to hit notes:

```bash
cd dashboard
python game_bridge.py                       # over USB, finds the board itself
python game_bridge.py --host 192.168.1.50   # over WiFi instead
python game_bridge.py --demo                # no board: fake flicks, to test the game
```

`docs/GAME_INPUT.md` covers setup over either transport, the diagnostic screen
(`ImuTest.tscn`), tuning, and the ESP32-S3 USB CDC traps.

**Dashboard**

```bash
cd dashboard
pip install -r requirements.txt
python main.py                # or: python main.py --port COM14
```

Pick the port and press **Connect**. The dashboard switches the board to its
machine-readable CSV mode automatically and restores the human-readable mode
when it disconnects.

**Over WiFi instead.** Give the board credentials once, over USB, and it
prints the address to connect to:

```
wifi ssid My Network
wifi pass hunter2
wifi connect          ->  OK wifi connected, ip 192.168.1.50, -47 dBm, udp port 3333
wifi auto on          # and join by itself at every boot from now on
```

Switch the dashboard's **Link** selector to *WiFi (UDP)*, type that address,
and connect. Nothing has to be configured on the board for it to know where to
send: it streams to whichever address last sent it a command, and connecting
is a command. Output goes to serial and UDP simultaneously, so a serial
monitor keeps working while the dashboard drives the board over the network.

---

## Verified on hardware

Measured on the board over USB CDC at 921600 baud, sitting still:

| Check | Result |
|---|---|
| Both devices enumerate | ICM-45605 at `0x68`, QMC6309 at `0x7C` with chip ID `0x90` |
| CSV stream integrity | 500/500 records parsed, 0 malformed |
| Stream timing | 100.0 Hz requested, dt mean 10.001 ms, sigma 0.001 ms |
| Higher rates | 200 Hz gives 201 Hz, 500 Hz gives 502 Hz |
| Magnetometer freshness | `mag_fresh` set on 100 % of records |
| Noise (still, 5 s) | accel sigma ~0.0015 g, gyro sigma ~0.04 dps, mag sigma ~0.2 uT |
| Gyro bias | +0.616 / -0.182 / +0.036 dps, repeatable across captures |
| INT1 on GPIO17 | confirmed live: FIFO watermark drove the interrupt count continuously |
| Control registers read back | CTRL1 `0x61`, CTRL2 `0x48`, exactly the requested config |
| NVS calibration | survives save / read-back / clear |
| Magnetometer self-test | X -52, Y -56, Z -46 LSB (see note below) |
| Dashboard against the board | 17/17 live checks pass, fusion level to ~1 degree |

**The magnetometer self-test is marginal, not clean.** The datasheet's pass
window is -50..-1 LSB on every axis; this part reads -52, -56 and -46. All
three bridges respond with the correct sign and similar magnitude, so the
signal chain is working -- the two out-of-range axes are 4 % and 12 % beyond a
limit published in a document still marked preliminary. The firmware reports
`FAIL` against the datasheet criterion rather than quietly widening the window,
and prints the per-axis values so the result can be judged rather than trusted.
Worth re-checking against a second board if you have one.

Because the board could not be moved, nothing motion-dependent was exercised
on real hardware: APEX tap / tilt / pedometer / free-fall events, flick, flip
and quick-movement detection, both position estimators, and the orientation
step that learns the mounting from directed movements are all verified only
against synthetic data (134 checks in `tests/test_motion.py`). They are
unproven on real motion.

**The UDP transport now works on hardware, but the board cannot sustain it.**
The board joined a real network, took commands over UDP and streamed records
back — auto-targeting, batched datagrams and command replies all confirmed
against the real firmware, not a simulation. What it cannot do is keep going:
after a few dozen samples the board resets, and with `wifi auto on` set it
reboots roughly once a second indefinitely.

The cause is electrical rather than in the firmware. The radio's transmit
bursts pull more current than the supply delivers, the brownout detector
fires, and the chip resets — which is why USB-only runs are flawless for hours
at 200 Hz while WiFi dies in seconds. **docs/WIFI.md** has the evidence, the
fixes (shorter cable, better port, powered hub, bulk capacitance) and the
recovery procedure. The firmware now refuses to auto-join after two boots that
died mid-join, so this can no longer leave a board with no way in.

## Known issue: APEX initialises once per IMU power-on

`startAPEX()` in TDK's library succeeds the first time and returns -1 for the
rest of the session. Measured call by call, `inv_imu_edmp_init_apex()` (the
first thing it invokes) is the only step that ever fails, and the header
documents it as returning an error "if EDMP is enabled". The vendor's
`startAPEX()` calls it *before* its own disable sequence and finishes by
enabling the engine, so it can never satisfy its own precondition twice.

None of these recover it: clearing the EDMP enable bit (before, after, or
immediately preceding the call, with the register read back as clear),
`inv_imu_soft_reset()`, `inv_imu_adv_device_reset()` via `begin()`, powering
both sensors down, longer settling delays, or `ESP.restart()` -- a CPU reset
leaves the IMU powered and holding its state. Only a genuine power cycle of
the IMU clears it.

**What this means in practice.** APEX works normally after a power-on or a
flash, which is the usual state. Switching to `fifo` or `wom` and back to
`stream` leaves the motion algorithms off until you unplug and replug. The
firmware does not pretend otherwise: it reports `imu.apex.active` in the
banner, prints a `NOTICE` when APEX could not be started, and brings the
sample stream up regardless -- accelerometer, gyroscope, temperature and
magnetometer are register reads that do not depend on APEX at all.

Two other defects were found and fixed along the way, both mine:

* every `startXxx(pin, handler)` call configures *and* starts APEX
  internally, so passing a handler to all seven ran `startAPEX()` eight times
  and only the first succeeded. TDK's own example passes no handler and
  configures once at the end; the firmware now does the same.
* a `fifo` to `stream` change used to wedge the driver inside an I2C
  transaction that never returned, taking the sketch down with no panic and
  no reboot. Re-running `begin()` on each mode change fixes it.

## Feature coverage

### ICM-45605

Driven through TDK's own `ICM45605` Arduino library, so the APEX algorithms
are the vendor implementations rather than reimplementations.

| Feature | Exposed as |
|---|---|
| Accelerometer | ODR 1.5625 Hz – 6.4 kHz, ±2/4/8/16 g |
| Gyroscope | ODR 1.5625 Hz – 6.4 kHz, ±15.625 – ±2000 dps |
| Die temperature | every sample, `25 + raw/128` °C |
| FIFO | `run fifo`, watermark interrupt on INT1 |
| Pedometer | step count, cadence, walk/run/unknown classification |
| Tilt detection | interrupt past 35° |
| Tap detection | count, axis and direction |
| Raise to wake | wake and sleep gestures |
| Free fall | with fall duration in ms |
| Low-g / High-g | threshold crossings |
| Wake on motion | `run wom`, accelerometer in low-power mode |

Two APEX features listed on the ICM-456xx family datasheet — **bring-to-see**
and **activity/inactivity detection** — are *not* available on this part. In
the vendor driver they are compiled only for the B1/C1 device families, and
the ICM-45605 is family A1. The same applies to the on-chip GAF sensor-fusion
quaternion outputs, which is why orientation is fused on the host instead.

This part also has no high-FSR mode (`INV_IMU_HIGH_FSR_SUPPORTED` is 0), so
32 g and 4000 dps are unavailable; the firmware rejects them rather than
silently clamping.

**INT1 is owned by exactly one subsystem at a time.** The vendor driver
reprograms the pin's interrupt sources for APEX, FIFO and wake-on-motion, so
`run stream | fifo | wom` selects between them instead of trying to combine
them.

### QMC6309

No vendor Arduino library exists, so `qmc6309.cpp` is written directly from
QST document 13-52-22 Rev. C. Every register in that datasheet is covered.

| Register | Field | Exposed as |
|---|---|---|
| `0x00` | Chip ID (`0x90`) | presence check in `begin()` |
| `0x01`–`0x06` | X/Y/Z output, 16-bit two's complement | `readRaw`, `readMicroTesla` |
| `0x09` | `DRDY`, `OVFL`, `ST_RDY`, `NVM_RDY`, `NVM_LOAD_DONE` | `readStatus` |
| `0x0A` | `MODE`, `OSR1`, `OSR2` | `mag mode`, `mag osr1`, `mag osr2` |
| `0x0B` | `SOFT_RST`, `ODR`, `RNG`, set/reset mode | `mag reset`, `mag odr`, `mag range`, `mag sr` |
| `0x0E` | `SELFTEST` | `mag selftest` |
| `0x13`–`0x15` | Self-test deltas | reported per axis with pass/fail |

Ranges are ±8 G (4000 LSB/G), ±16 G (2000 LSB/G) and ±32 G (1000 LSB/G);
output is converted to microtesla. Self-test passes when every axis delta
lands in the datasheet's −50…−1 LSB window.

---

## Protocol and transports

Line-oriented ASCII, carried over USB CDC at 921600 baud and over UDP (port
3333 by default) at the same time, with commands accepted from either. The
first character types the record, so the same stream is readable by a person
and trivially parsed by the dashboard.

```
D,<t_us>,ax,ay,az,gx,gy,gz,mx,my,mz,temp_c,mag_fresh
E,<t_us>,<event>,<key=value ...>
I,<key>,<value>
OK / OK <text> / ERR <text>
```

`mode pretty` (the default) swaps the `D` records for aligned human-readable
blocks; `mode csv` is what the dashboard uses. Data records carry **raw**
readings by default so the host can compute a calibration from them —
`csvcal on` applies the board's stored calibration instead.

Every line the firmware produces goes through one sink that fans it out to
whichever transports are enabled, so `sink serial | udp | both` is the only
thing that decides where output goes and no code that produces output knows
about transports at all. Over UDP, lines are coalesced into datagrams of up to
1200 bytes, flushed when the buffer fills or the oldest line in it turns 10 ms
old — so batching never costs more than 10 ms of latency and a datagram never
fragments. Datagrams may be dropped or reordered and nothing retransmits them;
every record carries a timestamp, so loss shows up as a gap rather than as bad
data.

Full details in `firmware/bbda_imu/protocol.h` and
[docs/USAGE.md](docs/USAGE.md); the command list is in `help`.

---

## Motion tab: position and gestures

**Position is dead reckoning and it drifts.** Acceleration is integrated twice
with no external reference, so error grows with the *square* of time spent
moving: a residual bias of 10 mg is roughly 5 cm after 1 second and 5 m after
10. Read it as relative motion over the last few seconds, never as a position
fix. The tab says so on screen and shows a live uncertainty.

The tab draws the path in 3D (10 cm grid, attitude triad at the tip) and as a
top-down 2D map of the world X-Y plane, with the origin and current estimate
marked.

Two estimators are selectable, so the difference can be seen rather than taken
on trust:

* **Kalman + ZUPT** (default) -- a 9-state error-state Kalman filter over
  position, velocity and body-frame accelerometer bias, the standard
  construction from the ZUPT-aided inertial navigation literature. The point
  of it is that standing still is treated as a *measurement* rather than a
  clamp: because the filter tracks the correlation between velocity, position
  and bias error, one zero-velocity observation corrects all three, pulling
  the position back along the direction the error is known to have taken and
  improving the bias estimate. The drawn path is corrected back over the
  segment that earned the correction. The uncertainty tile is the filter's own
  covariance, not a rule of thumb.
* **Simple integrator** -- rectangular steps, a hard velocity clamp when
  still, a fixed-gain bias leak. Kept because it is easy to reason about and
  to check the other one against.

**Flick detection** reports a short sharp rotation and which direction it went.
It is a state machine rather than a threshold, so it reports the peak of the
whole event instead of firing on the leading edge where the direction is still
ambiguous. It names the direction one of three ways: as the nearest board axis
and sign (six answers), as one of N equal sectors of a plane of board axes, or
-- the default, and the one to use for anything a person or a game consumes --
as **degrees clockwise from the board's front**, 0 up, 90 right, 180 down, 270
left, so six directions come out as 0, 60, 120, 180, 240 and 300. That last one
reports where the flick *went* rather than the axis it turned about, which is
90 degrees away from it: a flick of the front upwards is a pitch, and a
gyroscope measures pitch about the sideways axis, so reporting the axis raw
would name that flick sideways and name the *roll* -- which is not a direction
at all -- up and down. The direction is instead taken from the velocity of the
board's front under that rotation, `v = omega x front`. Up and down are
pitches, left and right are yaws, and a roll about the front is refused because
it leaves the front pointing exactly where it was. Which axis is the front is
selectable and has to match the board; easy mode's orientation step makes +X
correct. 4, 6, 8 and 12 sectors are offered. Ambiguity is rejected rather than
guessed at: in axis mode the *dominance* floor of 0.8 sits deliberately above
the 0.707 an exact 45-degree diagonal scores, in the plane modes the same floor
requires the rotation to lie in the chosen plane at all, and in bearing mode it
requires the rotation to have swung the front rather than twisted it -- while a
second test rejects anything landing on the boundary between two sectors.

**Quick-movement detection** is the translation counterpart: a shove, swipe or
lift, named *forward / left / back / right / up / down* once easy mode has
established which way the board faces, or divided into any number of sectors
like the flick detector. It reports direction, peak speed, distance and
duration.

Two things about it are worth knowing because they look like details and are
not. The direction comes from the peak of the *integrated velocity*, not from
the acceleration: a deliberate move is a push followed by an equal brake whose
net acceleration is zero, so reading direction from acceleration would report a
hard stop as having gone backwards. And the board must be at rest before a move
can start, because integrating velocity from an unknown starting velocity gives
an answer that is wrong and looks right.

**Flip detection** reports a settled change in which face points up, so turning
the board over slowly registers as reliably as throwing it over.

All of these run on the host, on calibrated and mount-corrected values, which
is what makes them tunable live.

## Calibration

The correction model is identical on both sides, so a calibration computed in
the dashboard behaves the same once pushed to the board:

```
gyro_out  = gyro_raw - gyro_bias
accel_out = (accel_raw - accel_bias) * accel_scale
mag_out   = mag_soft @ (mag_raw - mag_bias)
```

### Easy mode

The **Easy mode** button on the Calibrate tab hands the whole thing over to a
guided screen for someone who has never calibrated an IMU. It replaces the
dashboard rather than adding another panel, gives one physical instruction at
a time, and never shows a piece of jargon.

It removes the three things that actually make calibration go wrong:

* **"Hold it steady" is enforced, not requested.** Capture only runs while the
  board is genuinely still, and a knock mid-capture throws the partial away
  and says so.
* **You never have to work out which face is up.** The six-position step reads
  gravity to recognise each side by itself, so the instruction is "put it on a
  side you have not done yet" and the checklist fills in as you go.
* **You never have to work out which way round the board is mounted.** Instead
  of choosing between 24 axis mappings, you slide the board forward, up, down,
  left and right, and it solves for the mapping that fits all five movements
  (Wahba's problem, solved by Kabsch's algorithm, then snapped to the nearest
  exact mapping). The redundancy is the point: it yields a per-movement
  residual, so the answer comes with a measure of how much to trust it.
* **It checks its own work.** The last step verifies four things that must be
  true of any correct calibration -- a still board reports no rotation,
  gravity is 1.00 g, Earth's field is 25-65 uT, and the movements agreed on
  the mounting -- and if one fails it names the step to redo rather than
  leaving you with a bad result that looks finished.

### Expert panel

In the dashboard's **Calibrate** tab, in order:

1. **Gyroscope bias** — leave the board still; the mean is the bias. The
   panel warns if the peak-to-peak spread says the board moved.
2. **Accelerometer, six positions** — capture the board with each axis
   pointing up and down. Each axis' ±1 g bracket gives its offset and gain.
3. **Magnetometer** — rotate through as many attitudes as you can. An
   ellipsoid is fitted to the cloud: its centre is the hard-iron offset, the
   matrix square root of its normalised quadratic form is the soft-iron
   correction. The scatter view shows the raw cloud and the corrected sphere
   together, and the fit reports its residual so a bad capture is obvious.
4. **Filter tuning** — Madgwick gain `beta` (responsiveness against noise)
   and `zeta` (gyro-bias tracking, best left at zero once step 1 is done).

**Push to board** writes the result into the ESP32's NVS so the board's own
output is corrected too. Calibrations also save and load as JSON, and a working
copy is kept at `~/.bbda/calibration.json` without being asked -- written the
moment any step produces a result, in easy mode and the expert panel alike, and
read back at startup. Stopping half way through, a crash, or the cable coming
out costs nothing but the step in progress. It is also the only copy that keeps
the mounting orientation, which Push deliberately does not send.

If the board stops sending -- a pulled cable, a reset, WiFi going away -- a red
banner says so within two seconds, on the dashboard and on the easy-mode screen
both. That matters more than it sounds: half these screens ask you to hold the
board perfectly still, and a frozen stream looks exactly like success.

## Tests

```bash
python dashboard/tests/test_math.py        # 23 checks
python dashboard/tests/test_motion.py      # 134 checks
python dashboard/tests/test_gamebridge.py  # 31 checks
```

`test_math.py` covers the ellipsoid fit, the six-position solve and the
Madgwick filter (convergence, each Euler axis, compass heading, the 6-axis
fallback, pure gyro integration, the dropped-sample guard).

`test_motion.py` covers stationary detection; both position estimators against
a known push, including the exactness of the trapezoidal form under constant
jerk and the fact that the two schemes agree on a symmetric move; the Kalman
filter learning a standing bias, pulling position back at a stop, and running
away with zero-velocity updates disabled; sector quantisation including
boundaries, wrapping, the clockwise convention and rejected configurations;
flick detection in axis mode, across 4, 6 and 8 sectors with the boundary and
out-of-plane rejections, and at all six bearings from the board's front with
the twist rejection; the calibration file round trip;
quick-move detection in every direction including lifts, the hard-stop case,
the rest requirement, and the nudge and noise rejections; flip detection; and
the frame solve against all 24 mountings, with noise, with a genuinely crooked
board, and against the inputs it must refuse.

`test_gamebridge.py` covers the bridge that feeds the game: the bearing-to-lane
conversion against the game's real lane layout, the fact that the default
sector offset centres flicks on lanes rather than boundaries and that an
unoffset grid would not, every direction surviving detection and JSON encoding
over a real loopback socket, strength scaling and saturation, the refusals (a
roll about the front, a flick landing on a lane boundary), and that sending
with no game listening does not take the bridge down.

None of them needs hardware or a display.

---

## Sources

- [ICM-45605 product page and datasheet, TDK InvenSense](https://invensense.tdk.com/products/motion-tracking/6-axis/icm-45605)
- [ICM-45605 / ICM-45686 user guide, AN-000478](https://invensense.tdk.com/wp-content/uploads/2024/07/AN-000478_ICM-45605-ICM-45686-User-Guide.pdf)
- [TDK InvenSense ICM45605 Arduino driver](https://github.com/tdk-invn-oss/motion.arduino.ICM45605)
- QST QMC6309 datasheet, document 13-52-22 Rev. C
