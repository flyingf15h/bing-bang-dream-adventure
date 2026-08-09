# Demo
<img width=90% alt="image" src="https://github.com/user-attachments/assets/60197b6f-13b8-43c3-ab1e-5cd978b65b20" />

<img width=45% alt="image" src="https://github.com/user-attachments/assets/1a9cd251-4646-421f-840c-63d1d7461880" />

<img width=45% alt="image" src="https://github.com/user-attachments/assets/ba1901ba-3d48-42c2-ab56-feaaa6430755" />

<img width=90% alt="image" src="https://github.com/user-attachments/assets/6856caf3-e814-4170-a9eb-1257b985db98" />

---

## Running the game

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

The title screen says which of three states you are in:

```
IMU ready — flick the board to start
bridge running, no board — Lost the board: ClearCommError failed
no IMU bridge — run  python dashboard/game_bridge.py
```

The middle one matters more than it looks. **A running bridge is not a
connected board**: on an ESP32-S3 with native USB the serial port is provided
by the sketch, so a reset or a knocked cable makes the port vanish while the
bridge sits there retrying. Everything downstream still looks alive — the game
is receiving datagrams, so the link is genuinely up — and `--simulate-flicks`
keeps landing hits throughout, because those never touch the board. Real
flicks, meanwhile, cannot register at all. That combination (simulated flicks
work, real ones do nothing) means the board, not the detector.

Without that line, a title screen that ignores flicks looks broken rather than
unplugged. Mouse and keyboard keep working throughout; none of this replaces
them.

---

## The arrow in the middle of the ring

While the bridge is feeding it, the game draws a 2D arrow at the centre of the
playfield showing what the board is doing:

* it **points** where the board is being swung, in the same angle convention
  and from the same centre as the lanes, so "the arrow points at lane 3" and
  "a flick would hit lane 3" are the same statement;
* it **grows** with how hard, reaching full length exactly when the swing
  passes the threshold a flick starts at — a full-length arrow means "that
  would have registered";
* it takes the **colour of the lane** it is pointing at, pink for the right
  hand's three and blue for the left's, matching the notes arriving there;
* it **flashes down the lane** when a flick lands, at the flick's own measured
  bearing rather than the smoothed one.

That combination answers the question a player actually asks when a flick does
not land, and the two answers look nothing alike: a **short** arrow in the
right place means the flick was too gentle, a **long** one pointing elsewhere
means the direction was wrong (see the `--front` section below).

The arrow only appears while datagrams are arriving, so its absence is itself
the "no board" indicator. `I` toggles it in game, and `O` cuts it down to
detected flicks alone — nothing on screen between them. On the bridge,
`--motion-hz 0` does the same thing from the other end, by stopping the records
that feed the live arrow.

These `motion` records are advisory and are never scored from — they say the
board is moving, not that the player meant a lane. Feeding them into the input
bus would hit a note for every wave of the hand and make the detector's
judgement about what counts as a flick irrelevant.

---

## The debug panel

Tick **IMU debug**, top right of the title screen or of the game. Everything
needed to set a board up is behind it, and it edits the bridge live — nothing
here is a copy of the tuning, it is the tuning, and every control shows what
the bridge says it is actually running rather than what was asked for.

| Section | Answers |
|---|---|
| **Link** | Is the bridge running, is a board behind it, how fast, how many flicks and refusals, and the last refusal in full |
| **Live** | Which way the board is being swung, which lane that is, and how the swing compares to the threshold |
| **Orientation** | The front axis, and a helper that works it out for you |
| **Direction check** | Whether flicks go where you aim them, and the correction if they do not |
| **Sensitivity** | Threshold, swing floor, lane margin, refractory |
| **Leniency** | How generously a flick that registered is matched to a note |
| **Accuracy** | Gyro bias at rest, and writing it to the board |
| **Display** | The arrow, the colour rule, and flicks-only drawing |
| **Settings file** | Save, export, import, defaults |

### Working out the front axis without guessing

The front axis is the thing that is wrong when flicks land in the wrong lane,
and "which way does the board's +X point" is a question about a silkscreen and
a right hand that almost nobody gets right first time. So don't answer it:

1. Pick a direction you can flick accurately — **up** is easiest.
2. Press **learn from my next flick**.
3. Flick that way, once.

The bridge measures the rotation, tries all six candidate axes, and reports the
one that turns what you just did into the direction you said. It shows the
error in degrees and every runner-up, so a close call is visible rather than
hidden behind a single answer. Press **apply** and it is set.

### The direction check

The front-axis helper answers "which axis is the front" from one flick. This
answers the question after it — *do my flicks actually go where I aim them* —
by measuring four and fitting one correction to all of them at once.

Press **start check** and flick up, right, down, left as it asks. The board is
taken off the input bus for the duration, so none of those four presses a
button, starts a song or scores anything; a check run from the title screen
stays on the title screen. It gives the board back after the fourth flick, on
**cancel**, or after 25 seconds of waiting.

It then lists every flick — what you aimed, what the board reported, how far
off — and says which of four things is true:

| It says | What it means | What it offers |
|---|---|---|
| directions are right | off by less than 8°, inside what a hand can aim | nothing to fix |
| flicks land *n*° clockwise/anticlockwise | the whole ring is turned, consistently | **apply** rotates every bearing back |
| left and right are mirrored | the frame's handedness is wrong | **apply** mirrors, then rotates |
| these four do not agree | no single correction explains them | fix `--front` first, then re-run |

The last one is the important one, and it is why this measures four flicks
rather than one. A wrong front axis does not turn the ring — it scrambles it,
so three directions can be right while the others are nonsense, and any single
flick looks like a small aiming error. Fitting all four at once separates "the
board is rotated" (fixable here) from "the board is wrong" (fixable in
Orientation, above).

**Mirroring is checked separately for the same reason.** A reflection cannot be
undone by a rotation, and it hides: get the handedness wrong and up and down
stay correct while left and right swap, so half the directions confirm that
everything is fine. The fit only reports a mirror when it explains the four
flicks *better* than a rotation does, because a rotation is the ordinary fault
and a reflection is the surprising one.

The correction is the game's, not the bridge's — it is applied where a bearing
arrives, in `ImuInput.game_angle_of()`, and saved with the other settings.
`bearing_to_game_angle()` stays the pure wire convention so the tests can go on
restating it. **clear correction** puts it back to using bearings exactly as
reported.

### Rest bias, and what "accuracy" means here

Bias is what the gyro reads while the board is perfectly still. It does not
make flicks harder to detect — they are hundreds of dps and bias is a fraction
of one — but it never stops, so it is what makes a resting board look like it
is slowly turning.

Put the board down, press **measure**, and leave it alone for two seconds. If
the verdict is anything but *good*, **write to board** folds the measurement
into the board's own stored calibration, where it applies to everything the
board talks to and survives a reboot. The write reads the board's current
calibration first and adds to it, because `cal gyro` replaces rather than
accumulates — measure twice and the second reading comes back near zero.

If the board moved during the measurement it says so and writes nothing, rather
than saving your hand movement as the calibration.

### Sensitivity

| Slider | Raise it when | Lower it when |
|---|---|---|
| flick threshold | stray movements register | real flicks are refused as too gentle |
| swing floor | rolls are being read as flicks | a flick with wrist roll in it is refused |
| lane margin | flicks land in a neighbouring lane | flicks near a lane edge are refused |
| refractory | the return stroke fires a second, opposite flick | fast charts drop inputs |

**These start almost all the way down, on purpose.** Reaching the rate
threshold is very nearly the whole test of whether a movement was a flick. That
is the honest rule for playing: if the board got up to speed, that was a flick,
and a movement thrown hard enough to be meant and then argued out of existence
by a quality test is — from the player's side — indistinguishable from a dead
sensor.

* The **flick threshold** is 110 dps. A wrist flick peaks in the high hundreds
  and even an unhurried one clears 150; what did not clear the old 150 was the
  tail of them, the tired ones and the ones a noisy gyro read low. A hand at
  rest reads under 25 dps even while holding something, so there is a long way
  down before this starts catching noise.
* The **swing floor** is 0.2, which tolerates nearly 80° of roll mixed into a
  flick. It now rejects only a movement that is *almost entirely* a roll — and
  that one it must, because a roll leaves the board's front pointing exactly
  where it was and so has no direction to report. It is the one refusal here
  that is about physics rather than about confidence.
* The **lane margin** is 0: off. The game no longer needs a flick to have
  landed cleanly inside one lane, because it matches a flick against every note
  within its aim tolerance (below) and takes the one that best explains it.
  Refusing here throws away an input the game could have scored.
* A movement may now last **1000 ms** rather than 700 and still count. A swing
  made from the elbow rather than the wrist stays above the off threshold for
  most of a second, and it is a swing by any reading except that one.

Raise the threshold if stray movements register, and the refractory if the
return stroke fires a second flick the opposite way. Those are the two that
still earn their keep.

### Leniency

Detection decides whether a movement was a flick and which way it went.
Leniency decides what happens next: which note that flick is allowed to reach.
It lives in the game, not the bridge, because only the game knows where the
notes are — nothing in this section is ever sent over the control socket.

| Slider | Default | What it does |
|---|---|---|
| aim tolerance | 75° | how far off a lane a flick may point and still reach the note in it |
| window stretch | 2.8 | what the hit windows are multiplied by, for flicks only |

**Aim tolerance.** Lanes are 60° apart, so 30 is the strict behaviour: a flick
is snapped to the lane it landed nearest and can only hit a note there. Above
30 it reaches past that lane — at the default 75 it reaches a lane and a
quarter either side, which makes four of the six lanes candidates for any one
flick. That sounds reckless and is not, because the tolerance only says what is
*reachable*; the weighting below says what is *likelier*, and a note further
from where the flick pointed has to be correspondingly better timed to take it.
It is the right trade for a hand-thrown gesture measured off a MEMS gyro: the
bearing is good to a lane, not to a degree.

The ceiling is 100°, past which opposite sides of the ring start reaching each
other and the direction stops meaning anything at all. If flicks are landing in
the *wrong* lane rather than merely missing, this is the wrong knob — run the
direction check, because no amount of tolerance fixes a mapping that is turned.

When two notes are both in reach, the flick takes the one that best explains
it: nearest in time, with being aimed wide counted against a note at 1.5 ms per
degree (`AIM_COST_MS_PER_DEG` in `node_2d.gd`). So a whole lane of aiming error
trades against 90 ms of timing error, and the note the flick really pointed at
wins unless it is most of a window away. Without that weighting a wide
tolerance would just take whatever note was nearest in time, which reads as the
game guessing.

**Window stretch.** A flick is not an instant. The bridge times it from the
peak of the rotation and subtracts the detection lag (see *Timing*, below), but
the peak of a gesture thrown with a whole arm is a broader thing than the
moment a key went down, and what is left is tens of milliseconds of jitter that
no calibration removes. The stretch applies to flicks alone — keys and clicks
are judged on the same windows as always — and it also holds notes open longer
before they are called a miss, or the widened window would be a fiction.

At the default 2.8 a flick has ±126 ms to land a PERFECT and ±308 ms to score
at all, against the keyboard's ±45 and ±110. The slider goes to 4.

Both sliders go back to strict: 30 and 1.0 score a flick exactly as this game
scored one before any of it existed.

### The colour rule

With **colour only registered hits** on (the default), colour on screen means
exactly one thing: that flick hit a note. The live arrow, refusals, and flicks
that were accepted but landed in an empty lane all draw grey. It turns "did
that count?" into something answerable at a glance, which is the question being
asked over and over while a board is being set up.

**Draw detected flicks only** (the `O` key) asks a different question. The
colour rule is about scoring; this is about detection. With it on, the ring
stays empty until a swing is strong and clean enough that the detector sends it
as a flick, and then shows that flick — whether or not there was a note where
it went. No live arrow following the board, no rest dot, no refusal mark.

It is the setting for an unsteady board. The live arrow tracks every wobble,
and that motion is exactly what is in the way of the question being asked:
which of my swings are actually registering. Leave the colour rule on with it
and the two answers separate cleanly — an arrow appearing at all means the
swing counted, and its colour means it also hit a note.

### Storing it

Settings save themselves to `user://imu_settings.cfg` the moment they change,
and are sent to the bridge again whenever it reconnects — so a board tuned once
stays tuned across runs and across bridge restarts.

**Export** writes JSON instead, which is the format to keep next to a
particular board, mail to somebody, or commit. **Import** reads one back and
sends it straight to the bridge. A file that only mentions some settings
changes only those.

**Migrations run on an older file, once each.** `format` 1 has its threshold,
swing floor and lane margin put back to the current defaults; `format` 2 has
the two leniency values put back. Either says so on the console. Those were chosen against a detector that refused
far more than this one does — a threshold raised to 500 dps to stop phantom
flicks is a hard flick and nothing else — and keeping them would mean the
retune reached everybody except the people who had already tried to fix it by
hand. Everything else in the file survives: front axis, refractory,
calibration, and every display setting.

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

**6. Does the game play, with the board connected?**

```bash
python game_bridge.py --port COM5 --simulate-flicks
```

flicks every lane in turn *while the real board streams*, so the whole path —
board, cable, detector, socket, lanes, scoring — runs with only the flicks
themselves faked. It is the step for testing the game side with no free hand,
and for telling a game-side fault apart from a detection one: if these land
and yours do not, the game is fine and the answer is in the two sections
below.

**7. Can you actually play it?** Pick **Flick Test** on the map screen. It is
32 notes at 100 BPM with no music and no video: one lane at a time twice each,
then once round the ring, then side to side, then back round the other way.
Nothing overlaps, nothing is a slide, and every note is on screen for a second
and a fifth before it has to be hit — so a note that goes by unhit is a flick
that did not land, not a chart that was too fast to read. It is the chart to
have open while moving the leniency sliders.

Real flicks still count while it runs. Only the live arrow is taken over, and
only for as long as each simulated swing lasts — pick the board up between
them and the arrow follows it as usual.

---

## Timing: why a flick carries a lag with it

A flick cannot be recognised until enough of it has happened, so the report
always trails the gesture. How far behind is the detector's main tuning
decision, and it used to be as far behind as possible: it waited for the
rotation to fall under the off threshold, which means waiting out the
deceleration and usually the start of the return stroke too.

It now stops measuring once the rate has fallen to `commit_fraction` of its own
peak — 0.6 by default. On a flick shaped like a half sine, which is close to
what a hand throws, that reports about 0.3 of the gesture after the peak
instead of 0.5, with 90% of the rotation already integrated. Measured on
synthetic flicks:

| flick duration | reported late by | was |
|---|---|---|
| 60 ms | 25 ms | 30 ms |
| 90 ms | 35 ms | 45 ms |
| 120 ms | 45 ms | 60 ms |
| 150 ms | 50 ms | 75 ms |

There is a second lag that used to be invisible, and it was much the larger of
the two. The host read the serial port with `read(4096)` against a 0.2 s
timeout, so it always waited out the full timeout and then handed over a fifth
of a second of samples at once. Measured on the board: samples arrived a median
of **101 ms** and up to **211 ms** after the board timestamped them. Nothing
compensated for it, because every timestamp in a flick record is the *board's*
and they are all equally stale — the delay is only visible by comparing the two
clocks. Reading whatever is waiting instead brings that to a median of **0.2 ms**,
worst case 0.5 ms, and what is left is measured per sample and added to
`lag_ms` as `transport_ms`.

The game's PERFECT window is ±45 ms on the keyboard, and ±126 ms for a flick
once the window stretch is applied. Left uncorrected, a leisurely 120 ms flick
would eat most of even the stretched window — and, worse, **the error is not a
constant**: the player chooses how long a flick lasts, so it moves from flick
to flick and no fixed `audio_offset_ms` can remove it. Widening the window
cannot fix that either; it only makes an arbitrary-feeling input arbitrary
within a larger range. The correction below is what makes the window mean
something.

So every flick carries `lag_ms`: the gap between its peak and its report
(`detect_ms`) plus how long the sample it was found in spent in transit
(`transport_ms`), both broken out in the record so a bad one can be attributed
rather than guessed at.
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

## How the direction is worked out

Two things decide it, and both were changed because both were wrong in ways
that only showed up as flicks landing in the wrong lane.

**The whole stroke, not one sample of it.** The direction used to come from the
single fastest sample of the gesture. That sample carries the full sensor
noise, it is chosen by where the peak happened to fall between two samples, and
a wrist does not turn about a fixed axis — the axis sweeps through the stroke,
so the fastest instant says where the board was going at that instant rather
than where the movement went. The gyroscope is now integrated from the first
sample of the stroke to the last, carried in the board axes the flick started
in, which is where the board *ended up* relative to where it *started*. On
synthetic flicks with 25 dps of noise that is **0.96° rms against 3.23°** for
the peak sample. The record carries `turn_deg` and `samples` so you can see how
much of a stroke the answer was averaged over.

**Gravity, not the board.** "Up" used to mean the board's own +Z. The board is
held in a hand, and a hand holds things crooked — so every degree the board was
rolled about the axis it points along rotated every reported direction by that
same degree. It is not a fixed offset that can be calibrated out, because it
changes with how the board happens to be held for that one flick. A gravity
tracker (a small complementary filter: propagate with the gyroscope, correct
with the accelerometer only when it can be believed) supplies real vertical in
board axes, and the flick's frame is levelled against it at the moment the
flick starts. Replaying one capture of six flicks thrown from a 25° crooked
grip:

| aimed at | 30° | 90° | 150° | 210° | 270° | 330° |
|---|---|---|---|---|---|---|
| levelled | 29.9 | 90.4 | 150.5 | 210.2 | 270.6 | 331.1 |
| board frame | 4.9 | 65.3 | 125.2 | 185.0 | 245.1 | 305.1 |

Every board-frame reading is off by exactly the 25° of grip, which against 60°
lanes is most of the way to the neighbour. `--no-level` on the replay tool puts
it back if you want to see it.

**This depends on the accelerometer being calibrated.** Vertical comes from
gravity, so a board whose stored calibration makes it read 3 g lying still
cannot find vertical at all — and the failure is silent, because the tracker
simply never engages and the old board-frame behaviour comes back. The bridge
now watches for it and says so by name. Check with `cal show` on the board that
`cal.accel_scale` is close to `1 1 1`; a value like 26 or 75 comes from a
six-position calibration where two of the positions were really the same face,
and `cal ascale 1 1 1` + `cal save` undoes it.

---

## Checking that directions are right

`dashboard/flick_check.py` answers the question nothing else does — "when I
flick up, does it think I flicked up" — which cannot be answered from inside
the detector, because only the person throwing the flick knows where it was
aimed.

```bash
python flick_check.py watch            # a dial that follows the board, live
python flick_check.py aim              # flick each lane, get an error table
python flick_check.py record run.jsonl # capture raw samples
python flick_check.py replay run.jsonl # re-detect over a capture
```

`watch` is the fastest check and needs no patience: swing the board up and the
marker goes to the middle, swing it right and it goes right. A wrong front axis
is visible in about four seconds this way, where the same fault seen only
through flicks landing in odd lanes looks like bad luck for as long as you are
willing to keep flicking.

`aim` uses **the front axis the game is set to**, read out of Godot's own
settings file, not a default of its own. That distinction cost a whole
measurement session once: run against the wrong front axis, the bearings are not
merely offset but scrambled — the plane the board's front sweeps through is the
wrong plane, so every direction is wrong by a different amount — and the run
comes back reporting eighty degrees of scatter and a confident diagnosis of
"mirrored", every word of which is about the flag rather than the board.

So `aim` now answers "which axis is the front" from the same flicks, before
anything else:

```
  front axis      accuracy   flicks it could read
  --------------------------------------------------
  +Y                1.4 deg   6 of 6  <- fits best
  -Y                1.4 deg   6 of 6  mirrored
  +Z               29.8 deg   4 of 6
  +X               48.0 deg   6 of 6  <- in use
```

That works because each flick carries both the whole stroke as one rotation
*and* the vertical at the moment it started, so the bearing for any candidate
front is a calculation rather than another six runs. An axis and its opposite
tie exactly — one of them reflected — and the un-mirrored one wins, because a
mirror is a correction the game would otherwise carry for ever to undo a guess
made here.

The axis in use has to be beaten by a wide margin before the tool says to change
it: with a dozen hand-thrown flicks two axes can land close together by luck,
and telling somebody to change a correct setting is worse than saying nothing.

`aim` then separates the two kinds of wrong, and that separation is the point:

* a **constant offset** — everything rotated by the same amount. This is a
  mounting angle or a wrong front axis, it is one number, and the game already
  corrects it (the debug panel's direction check sets `bearing_offset_deg`).
  It is not an accuracy problem.
* the **spread left after taking that offset out**. This is the real
  directional accuracy, it cannot be calibrated away, and it is the number to
  compare against a target like "within three degrees".

Reporting only the raw error mixes the two, and makes a perfectly consistent
board with a 40° mounting error look hopeless while a board with no offset and
30° of scatter looks fine.

`record` and `replay` exist so tuning is not guesswork: capture one set of
flicks, replay it through as many settings as you like, and compare on
identical data. A change measured against a fresh set of hand-thrown flicks is
measured against the hand as much as against the change.

---

## Two boards, one per note colour

The blue notes are the left hand and the pink ones are the right — that is what
the charts already call them (`"hand": "left"` / `"right"`). Notes marked `any`,
and the gold bonus notes, are open to either.

```bash
python game_bridge.py --board left=COM7 --board right=COM9
python game_bridge.py --board blue=COM7:+Y --board pink=COM9:-X
python game_bridge.py --two-boards        # find both; the first gets blue
```

Both boards run in **one** process and post to the **same** game port. The game
has one input path, one detector tuning and one set of hit windows; splitting
either across two processes would mean keeping two copies of the part that most
needs a single source of truth. What each board keeps to itself is the part that
really is per board:

| per board | shared |
|---|---|
| serial port or IP | threshold, swing floor, margin, refractory |
| front axis (`--board hand=target:+Y`) | commit fraction, sector layout |
| control port (base, base+1, …) | lane tolerance, window stretch |
| aim correction (direction check) | sample rate, calibration |

Every record a tagged board sends carries `"hand"`, not just its flicks — a
refusal, a status or a live motion record from one board has to be
distinguishable from the other's, or "one of your two boards has stopped" is not
something the game can say.

**The aim correction is per board and has to be.** Two boards are two mountings
held in two hands; a correction fitted against one is wrong for the other by
however differently it happens to sit. Run the debug panel's direction check
once with each board in hand — it records which board threw the flicks and
stores that board's answer, and it ignores flicks from the other one mid-run
rather than solving for a correction that fits neither.

**Both boards are held the same way up.** Nothing mirrors the second one: the
front axis defaults to the same axis for both, and `--board hand=target:+Y` is
there for the case where the two units are physically mounted differently, not
because the right hand is expected to be a mirror of the left.

A board with no hand named plays everything, exactly as before. That is still
the normal setup and nothing about it changed.

### What the game shows with two of them

Everything that describes a board is per board, because the case worth showing
is the one where the two disagree — and a readout averaged across them is at
its most misleading exactly when someone is trying to work out why half the
chart stopped scoring.

* **Two arrows**, planted either side of the ring's centre, blue on the left and
  pink on the right. Each follows its own board, keeps its own flash when a
  flick lands, and carries its own refusal line — prefixed `blue:` or `pink:` so
  advice about one hand is not read as advice about the other. With one board
  there is one arrow in the middle, as before, and it still takes the colour of
  the lane it points at.
* **Per-board link, rate, flick and refusal counts** in the debug panel and in
  `ImuInput.debug_line()`, side by side.
* **A board that stops talking is noticed on its own.** The bridge's own link
  timeout cannot see it — the port keeps receiving because the *other* board is
  still sending — so the game times out each board separately and says which
  colour has gone quiet. Its arrow disappears; the other keeps playing.
* **Dropped datagrams are counted per board.** Each bridge numbers its own from
  zero, so a single counter would read two interleaved sequences as a flood of
  losses on a link that has not lost anything.

---

## When a flick is detected and scores nothing

The bridge already explains the movements *it* turns down. The game now explains
the ones it accepted and did not score, which used to be pure silence — and
silence is indistinguishable from a board that is unplugged, a bridge that is
not running, or a socket that would not bind. It appears wherever bridge
refusals appear, and on the console:

```
[imu] no note for that flick: aimed 71 deg wide -- flick went to 47 deg,
      nearest blue note is the lane at 120, and the limit is 75.
      If every flick does this by about the same amount, run the direction check.
[imu] no note for that flick: 214 ms late -- the direction was right,
      the window is 308 ms.
[imu] no note for that flick: that flick was fine -- there were no pink notes
      left to hit
```

Those three are the whole space of answers, and they point at three different
fixes: the direction check, the audio offset, and nothing at all.

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
rotated from the lane you aimed at, rather than as scatter. The panel's
**direction check** tells the two apart properly, and fixes the rotated case
outright — it is the thing to run first when lanes are wrong.

If the arrow is right but the lane is off by half a lane, the sector grid is
misaligned: `--sector-offset` rotates it, and 30 degrees is the default
because the game's lanes sit at 0/60/…/300 counter-clockwise from screen
right, so a grid aligned to zero would put every flick on a boundary.

**There is no lane at the top of the ring.** The lanes are at 0/60/…/300, so
90 — straight up — falls exactly between two of them, and a flick straight up
is genuinely ambiguous. It used to be refused outright for that reason, and it
surprised everyone once. It is now mostly not: the lane margin is near zero, so
the bridge reports it, and both neighbouring lanes are inside the game's aim
tolerance — whichever has a note due decides it, and if both do, the one nearer
in time wins. Aim between two lanes deliberately and it is still a coin toss;
aim at one and miss by a few degrees and it now lands.

---

## When flicks are not detected

**Start by reading what the bridge says.** A movement that is seen and refused
prints its own explanation, and so does one that never reached the threshold:

```
[refused]   31.4deg  mostly a roll -- only 0.11 of the turn swung the board's
                     front, needs 0.20. If every direction does this, --front
                     names the wrong axis; if only some do, --swing 0.10
[refused]   88.0deg  too gentle -- peaked 71 dps, needs 110. Flick from the
                     wrist, or --threshold 60
```

The same sentence appears in the game, under the arrow, so you do not have to
watch a console while playing.

Two of the three refusals above are now rare by default: the lane-margin test
is off, and the swing floor only fires on a movement that is essentially a
roll. What is left is mostly "too gentle", which is the one worth seeing.

This exists because silence is the one answer that cannot be read. A rejected
flick used to produce nothing at all, which is exactly what an unplugged board
produces — and the "too gentle" case is worse still, because the detector never
wakes up for it and so cannot even know it happened. The bridge watches for
those separately, from the moment the board starts moving until it stops.

**If nothing is printed at all**, the board is not streaming: check the title
screen's status line, or `[link]` in the bridge's own output.

| Symptom | Cause | Fix |
|---|---|---|
| `--monitor` says `under` | not a sharp enough movement | flick from the wrist, or `--threshold 80` |
| detected on the bench, refused in play | almost pure roll — no direction in it | check `--front` first; `--swing 0.1` if only some directions do it |
| some directions work, others never | almost always the wrong `--front` axis | run **learn from my next flick** in the panel |
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
| `--threshold` | 110 | dps that starts a flick |
| `--swing` | 0.2 | how much of the rotation must be a swing, not a roll |
| `--margin` | 0 | how far from a lane boundary a flick must land |
| `--refractory` | 200 | ms ignored after a flick |
| `--sectors` | 6 | lanes |
| `--sector-offset` | 30 | degrees the sector grid is rotated |
| `--rate` | 400 | samples per second asked of the board |
| `--game-port` | 3334 | must match the game's `--imu-port=` |

Game-side flags: `--no-imu` disables the listener entirely, `--imu-port=N`
moves it.

---

## Wire format

One JSON object per datagram, UTF-8, to `127.0.0.1:3334`.

```json
{"v":1,"type":"hello","transport":"serial","target":"COM5","sectors":6,"rate_hz":400}
{"v":1,"type":"flick","seq":12,"t":91.42,"peak_t":91.37,"lag_ms":46.0,
 "detect_ms":45.7,"transport_ms":0.3,"host_t":1712.3,"bearing":88.7,"sector":1,
 "strength":0.61,"peak_dps":464.0,"dominance":0.93,"duration_ms":92.0,
 "turn_deg":28.2,"samples":33}
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
