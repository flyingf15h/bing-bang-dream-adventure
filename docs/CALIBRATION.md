# Calibration and axis alignment

> Generated from `dashboard/bbda/guide.py`, which is also what the
> dashboard shows behind *Calibrate -> Show the step-by-step guide*.
> Edit that file, then re-run `python docs/build_guide.py`.

**In a hurry, or new to this?** Use *Easy mode* on the Calibrate
tab instead. It runs the same measurements from its own screen, one physical
instruction at a time, and works out for itself which side of the board is up
and when you are holding it still. This page is the reference for what it is
doing and how to do it by hand.

## Before you start

Do this on a wooden or plastic table, away from laptops, speakers, phones,
motors and steel furniture. A steel table leg will corrupt the magnetometer
calibration in a way that looks like a successful fit.

Let the board warm up for a minute or two first. Both bias and offset move
while the die temperature is still climbing.

## 1. Gyroscope bias -- 10 seconds

Set the board down and **do not touch it**. Press *Capture*. The
average reading is the bias, because a stationary gyroscope should read zero.
If the panel reports a large peak-to-peak spread it saw you nudge the table;
run it again.

## 2. Accelerometer, six positions -- 2 minutes

Hold the board still in each of six orientations and capture. Each axis
needs one reading with it pointing at the ceiling and one at the floor: those
bracket +/-1 g, and the midpoint is the offset while half the span
is the gain error.

Rest each edge against something solid. Held in the air by hand, a slow
drift of a few degrees turns into a gain error you will never see again.

## 3. Magnetometer -- 1 minute

Press *Start collecting* and rotate the board slowly through as many
different attitudes as you can reach. Figure-of-eight motions, then tumble it
about each axis in turn. Watch the coverage bar: it counts how many octants of
the sphere you have visited, and a fit from one axis of rotation only is
worthless.

Press *Fit ellipsoid*. The orange cloud is raw, the green cloud is
corrected. A good result is a green sphere centred on the white origin dot with
a residual under about 2 %. If the residual is high, or the fit is
rejected, you either did not cover enough of the sphere or there is iron
nearby.

## 4. Filter tuning

**Beta** sets how hard the accelerometer and magnetometer pull the
estimate back. Low beta is smooth and slow -- gyro drift shows before it
gets corrected. High beta tracks quickly but drags sensor noise into the
attitude. Around 0.03-0.1 suits a board sitting still or moving slowly;
raise it while the board is being handled.

To tune it honestly, compare the fused *Roll / Pitch* against the
*Accel-only roll / pitch* tile. With the board still they should agree.
Beta is right when they agree at rest and the fused value does not visibly lag
when you move.

Leave **zeta** at zero once step 1 is done. It tracks gyro bias, and with
a real bias calibration in place it only adds a second feedback loop to fight
the first.

## 5. Making the 3D view match the real board

This is a display convention, not a sensor correction: the mapping is
applied on the host and is never pushed to the board, so the board's own
printed readings always stay in the axes silkscreened on the PCB.

**Easy mode can do this for you.** Its fourth step asks you to slide the
board forward, up, down, left and right, and reads the mapping off those five
movements -- it fits the single rotation that best carries each measured
direction onto the direction you were asked to move in, then snaps that to the
nearest of the 24 exact mappings and reports how far it had to snap. If the
number it gives is more than about 20 deg, either your board really is mounted
at an angle or the movements were not straight. Doing it by hand instead means
working through these four checks; after each one, if the model does the
opposite of the board, change the mapping and start again.

1. **Lay the board flat, component side up.** The model should lie flat
with its Z arm pointing at the sky. The live line under the selector should
read *+Z points up*.
1. **Tilt the front edge down.** The model should pitch nose-down by the
same amount. Compare against the *Accel-only roll / pitch* tile, which is
computed straight from gravity and needs no filter.
1. **Roll the board to the right.** The model should roll right.
1. **Rotate the board clockwise seen from above.** The heading should
*increase* -- 0 deg is magnetic north, 90 deg is east.

Checks 1-3 use gravity only, so they work even with the magnetometer
uncalibrated. Only check 4 depends on the magnetometer, so if heading is the
only thing wrong, go back to step 3 rather than changing the mapping.

Only the 24 mappings that are genuine rotations are offered. The other 24
signed permutations are mirrors: they would flip the handedness of the frame,
which reverses the sense of every gyroscope reading and makes the model spin
backwards while looking almost right at rest.

## 6. Save it

*Push to board* writes bias, scale and iron correction into the ESP32's
NVS so the board's own output is corrected too. *Save JSON* keeps a host
copy, including the axis mapping, which *Push* deliberately does not
send.

Easy mode has the same two, as *Save to the board* and *Save to a
file* on its last step, and reads a file back with *Load a saved
file* on its first. Loading lands you on the checking step rather than at
the end: a file is a claim about a board, and those checks are what tell you
it is still true of this board on this desk.

**None of this is needed to keep your work.** A working copy is written
to *~/.bbda/calibration.json* the moment any step produces a result, here
and in easy mode alike, and read back at startup. Stopping half way, a crash
or the cable coming out costs nothing but the step in progress. Saving to a
file is for named copies elsewhere; pushing is for the board's own output.

## What this cannot fix

Accelerometer cross-axis misalignment, gyroscope scale-factor error and
temperature drift are all left uncorrected -- measuring them needs a rate
table and a thermal chamber. The six-position method also assumes gravity is
exactly 1 g, which is good to about 0.3 % anywhere on Earth.
