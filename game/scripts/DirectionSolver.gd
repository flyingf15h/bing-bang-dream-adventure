class_name DirectionSolver
extends RefCounted
## Fits one aim correction to a set of thrown flicks: which single rotation,
## with or without a mirror, best explains where all of them landed.
##
## Extracted from the old ImuDebugPanel's direction check so the in-game
## calibration wizard (CalibrationWizard.tscn) and the Advanced settings tab
## can both drive the same four-flick check without a second copy of the
## fit -- this is exactly the piece of logic in this project most likely to
## silently drift if it existed twice, since a rewritten wizard "fixing"
## unrelated UI text would have no reason to notice a subtly different
## rotation formula living a few files away.

## Worst per-flick disagreement, in degrees, still called a consistent
## answer. An eighth of a turn is roughly what a hand throwing four flicks in
## a hurry produces; past a quarter they are no longer describing one
## mapping at all.
const TIGHT_DEG := 20.0
const LOOSE_DEG := 45.0

## Rotation small enough not to be worth correcting. Well inside a lane, and
## inside what a person can aim by hand, so "fixing" it would be fitting the
## correction to the throw rather than to the board.
const NEGLIGIBLE_DEG := 8.0


## How far a flick landed from where it was aimed, in radians, once `flip`
## and `offset_deg` have been applied to it. Zero means the correction under
## test puts that flick exactly where the player said they were throwing it.
static func sample_error(sample: Dictionary, flip: bool, offset_deg: float) -> float:
	var measured: float = float(sample["got"])
	if flip:
		measured = -measured
	return angle_difference(deg_to_rad(measured + offset_deg),
		deg_to_rad(float(sample["expect"])))


## One candidate fit (with or without a mirror): the single rotation that
## best explains every sample at once, and the worst any one disagrees with
## it. Averaged as vectors rather than as numbers, because these are angles:
## the mean of 350 and 10 is 0, and arithmetic makes it 180.
static func _fit(samples: Array, flip: bool) -> Dictionary:
	var sx: float = 0.0
	var sy: float = 0.0
	for sample in samples:
		var error: float = sample_error(sample, flip, 0.0)
		sx += cos(error)
		sy += sin(error)
	var mean: float = atan2(sy, sx)
	var spread: float = 0.0
	for sample in samples:
		spread = maxf(spread, absf(angle_difference(
			mean, sample_error(sample, flip, 0.0))))
	return {"offset": fposmod(rad_to_deg(mean), 360.0), "spread": rad_to_deg(spread)}


## Solves ``samples`` (each ``{"name": String, "expect": float, "got": float}``,
## degrees clockwise from up) and returns:
##
##   offset, flip, spread   -- the fit itself, as Settings.set_aim() wants it
##   lines                  -- one line per sample, for showing the throws
##   verdict                -- a sentence explaining what the fit means
##   apply_worthy           -- whether offering "apply" makes sense
static func solve(samples: Array) -> Dictionary:
	var direct: Dictionary = _fit(samples, false)
	var mirrored: Dictionary = _fit(samples, true)
	# The mirror has to explain the flicks *better*, not merely explain them.
	# A rotation is the ordinary fault and a reflection is the surprising
	# one, so it has to earn being named -- and with four flicks the two
	# fits are never far apart by chance.
	var best: Dictionary = direct
	var flip := false
	if float(mirrored["spread"]) < float(direct["spread"]) - 5.0:
		best = mirrored
		flip = true
	var offset: float = float(best["offset"])
	var spread: float = float(best["spread"])
	# Signed, so it can be said as a direction rather than as a number.
	var turn: float = rad_to_deg(angle_difference(0.0, deg_to_rad(offset)))

	var lines: PackedStringArray = []
	for sample in samples:
		lines.append("%s: aimed %.0f, read %.0f  (%+.0f off)" % [
			String(sample["name"]), float(sample["expect"]), float(sample["got"]),
			-rad_to_deg(sample_error(sample, false, 0.0))])

	var verdict: String = ""
	var apply_worthy := false
	if spread > LOOSE_DEG:
		verdict = ("These four do not agree with each other -- one is %.0f deg "
			+ "from the best fit -- so no single correction can fix them. That "
			+ "is almost always the front axis: learn it from a flick, then run "
			+ "this again. If it persists, throw them harder; a lazy flick has "
			+ "no clear direction to read.") % spread
	elif flip:
		verdict = ("Left and right are mirrored, and the ring is turned %.0f "
			+ "deg on top of that. A rotation alone cannot undo a mirror, "
			+ "which is exactly why this is worth measuring.") % absf(turn)
		apply_worthy = true
	elif absf(turn) <= NEGLIGIBLE_DEG:
		verdict = ("Directions are right: off by %.0f deg, which is inside "
			+ "what a hand can aim. Nothing to fix.") % absf(turn)
	else:
		verdict = ("Flicks land %.0f deg %s of where you aim them, and do it "
			+ "consistently. Applying this turns every bearing back.") % [
			absf(turn), "anticlockwise" if turn > 0.0 else "clockwise"]
		apply_worthy = true
	if spread > TIGHT_DEG and spread <= LOOSE_DEG:
		verdict += (" The four disagree by up to %.0f deg, so this is a rough "
			+ "fit -- worth running once more.") % spread

	return {
		"offset": offset, "flip": flip, "spread": spread,
		"lines": lines, "verdict": verdict, "apply_worthy": apply_worthy,
	}
