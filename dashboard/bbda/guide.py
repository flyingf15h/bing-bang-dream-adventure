"""The calibration and axis-alignment guide, shown in-app.

Kept as one string so the in-app dialog and docs/CALIBRATION.md cannot drift
apart -- the doc is generated from this.
"""

GUIDE_HTML = """
<p><b>In a hurry, or new to this?</b> Use <i>Easy mode</i> on the Calibrate
tab instead. It runs the same measurements from its own screen, one physical
instruction at a time, and works out for itself which side of the board is up
and when you are holding it still. This page is the reference for what it is
doing and how to do it by hand.</p>

<h3>Before you start</h3>
<p>Do this on a wooden or plastic table, away from laptops, speakers, phones,
motors and steel furniture. A steel table leg will corrupt the magnetometer
calibration in a way that looks like a successful fit.</p>
<p>Let the board warm up for a minute or two first. Both bias and offset move
while the die temperature is still climbing.</p>

<h3>1. Gyroscope bias &mdash; 10 seconds</h3>
<p>Set the board down and <b>do not touch it</b>. Press <i>Capture</i>. The
average reading is the bias, because a stationary gyroscope should read zero.
If the panel reports a large peak-to-peak spread it saw you nudge the table;
run it again.</p>

<h3>2. Accelerometer, six positions &mdash; 2 minutes</h3>
<p>Hold the board still in each of six orientations and capture. Each axis
needs one reading with it pointing at the ceiling and one at the floor: those
bracket &plusmn;1&nbsp;g, and the midpoint is the offset while half the span
is the gain error.</p>
<p>Rest each edge against something solid. Held in the air by hand, a slow
drift of a few degrees turns into a gain error you will never see again.</p>

<h3>3. Magnetometer &mdash; 1 minute</h3>
<p>Press <i>Start collecting</i> and rotate the board slowly through as many
different attitudes as you can reach. Figure-of-eight motions, then tumble it
about each axis in turn. Watch the coverage bar: it counts how many octants of
the sphere you have visited, and a fit from one axis of rotation only is
worthless.</p>
<p>Press <i>Fit ellipsoid</i>. The orange cloud is raw, the green cloud is
corrected. A good result is a green sphere centred on the white origin dot with
a residual under about 2&nbsp;%. If the residual is high, or the fit is
rejected, you either did not cover enough of the sphere or there is iron
nearby.</p>

<h3>4. Filter tuning</h3>
<p><b>Beta</b> sets how hard the accelerometer and magnetometer pull the
estimate back. Low beta is smooth and slow &mdash; gyro drift shows before it
gets corrected. High beta tracks quickly but drags sensor noise into the
attitude. Around 0.03&ndash;0.1 suits a board sitting still or moving slowly;
raise it while the board is being handled.</p>
<p>To tune it honestly, compare the fused <i>Roll / Pitch</i> against the
<i>Accel-only roll / pitch</i> tile. With the board still they should agree.
Beta is right when they agree at rest and the fused value does not visibly lag
when you move.</p>
<p>Leave <b>zeta</b> at zero once step 1 is done. It tracks gyro bias, and with
a real bias calibration in place it only adds a second feedback loop to fight
the first.</p>

<h3>5. Making the 3D view match the real board</h3>
<p>This is a display convention, not a sensor correction: the mapping is
applied on the host and is never pushed to the board, so the board's own
printed readings always stay in the axes silkscreened on the PCB.</p>
<p><b>Easy mode can do this for you.</b> Its fourth step asks you to slide the
board forward, up, down, left and right, and reads the mapping off those five
movements &mdash; it fits the single rotation that best carries each measured
direction onto the direction you were asked to move in, then snaps that to the
nearest of the 24 exact mappings and reports how far it had to snap. If the
number it gives is more than about 20&deg;, either your board really is mounted
at an angle or the movements were not straight. Doing it by hand instead means
working through these four checks; after each one, if the model does the
opposite of the board, change the mapping and start again.</p>
<ol>
<li><b>Lay the board flat, component side up.</b> The model should lie flat
with its Z arm pointing at the sky. The live line under the selector should
read <i>+Z points up</i>.</li>
<li><b>Tilt the front edge down.</b> The model should pitch nose-down by the
same amount. Compare against the <i>Accel-only roll / pitch</i> tile, which is
computed straight from gravity and needs no filter.</li>
<li><b>Roll the board to the right.</b> The model should roll right.</li>
<li><b>Rotate the board clockwise seen from above.</b> The heading should
<i>increase</i> &mdash; 0&deg; is magnetic north, 90&deg; is east.</li>
</ol>
<p>Checks 1&ndash;3 use gravity only, so they work even with the magnetometer
uncalibrated. Only check 4 depends on the magnetometer, so if heading is the
only thing wrong, go back to step 3 rather than changing the mapping.</p>
<p>Only the 24 mappings that are genuine rotations are offered. The other 24
signed permutations are mirrors: they would flip the handedness of the frame,
which reverses the sense of every gyroscope reading and makes the model spin
backwards while looking almost right at rest.</p>

<h3>6. Save it</h3>
<p><i>Push to board</i> writes bias, scale and iron correction into the ESP32's
NVS so the board's own output is corrected too. <i>Save JSON</i> keeps a host
copy, including the axis mapping, which <i>Push</i> deliberately does not
send.</p>
<p>Easy mode has the same two, as <i>Save to the board</i> and <i>Save to a
file</i> on its last step, and reads a file back with <i>Load a saved
file</i> on its first. Loading lands you on the checking step rather than at
the end: a file is a claim about a board, and those checks are what tell you
it is still true of this board on this desk.</p>
<p><b>None of this is needed to keep your work.</b> A working copy is written
to <i>~/.bbda/calibration.json</i> the moment any step produces a result, here
and in easy mode alike, and read back at startup. Stopping half way, a crash
or the cable coming out costs nothing but the step in progress. Saving to a
file is for named copies elsewhere; pushing is for the board's own output.</p>

<h3>What this cannot fix</h3>
<p>Accelerometer cross-axis misalignment, gyroscope scale-factor error and
temperature drift are all left uncorrected &mdash; measuring them needs a rate
table and a thermal chamber. The six-position method also assumes gravity is
exactly 1&nbsp;g, which is good to about 0.3&nbsp;% anywhere on Earth.</p>
"""
