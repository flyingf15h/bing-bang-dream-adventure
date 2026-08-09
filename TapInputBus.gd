extends Node
## Central hub for rhythm "tap" events.
##
## Nothing in gameplay should read mouse/touch/IMU input directly. Instead,
## every input source calls report_tap() here, and gameplay listens to the
## `tap` signal. That way, once the IMU is finished, it plugs in as just
## another caller of report_tap() (e.g. from a serial/UDP listener node)
## without any changes to title screen, gameplay, or scoring code.

signal tap(event: TapEvent)

## What gameplay did with a tap, once it has judged it: true if it landed on a
## note, false if it was a well-formed input that hit nothing.
##
## Here rather than inside gameplay because two things outside scoring need it
## and neither should have to read the note list: the arrow, which only takes a
## lane's colour for a hit, and the debug panel, which is often open on a
## screen where there is no scoring at all.
signal tap_judged(source: String, hit: bool)

class TapEvent:
	var source: String
	var timestamp_ms: int
	var screen_position: Vector2
	var strength: float # 0.0-1.0, e.g. IMU impact force; 1.0 for mouse/touch

	## Direction of the input in degrees counter-clockwise from screen right,
	## or NAN when the source has no direction of its own.
	##
	## A pointer names a direction by *where it is*, so mouse and touch leave
	## this unset and gameplay reads the angle from screen_position. The IMU
	## has no position to read -- a flick is a rotation, and the board is not
	## anywhere on the screen -- so it names the direction outright and leaves
	## screen_position at zero. Consumers should prefer this when it is set.
	var direction_deg: float = NAN

	## How long before this event the input really happened, in milliseconds.
	##
	## Zero for a pointer: a click is known the instant it happens. Not zero
	## for the IMU, because a flick cannot be recognised until it is over, so
	## the report always trails the gesture by about half its duration. Scoring
	## must subtract this or every flick reads late -- and by a varying amount,
	## since the player chooses how long a flick lasts, which is why it cannot
	## be folded into a fixed latency setting.
	var lag_ms: float = 0.0

	## Which note colour this input may hit: "left" for the blue notes, "right"
	## for the pink, or "" for an input that may hit either.
	##
	## Empty for mouse, touch and keys, and for a single board -- one input
	## device plays the whole chart, which is the arrangement this game had
	## before there were two of them and is still the normal one. Set only when
	## a board has been told which hand it is, and then it is a restriction:
	## that board's flicks reach that colour and the notes marked `any`, and
	## nothing else. A player holding two boards has one in each hand, and a
	## flick from the left hand landing on a pink note would be scoring the
	## movement they did not make.
	var hand: String = ""

	func _init(p_source: String, p_screen_position: Vector2, p_strength: float,
			p_direction_deg: float = NAN, p_lag_ms: float = 0.0,
			p_hand: String = "") -> void:
		source = p_source
		timestamp_ms = Time.get_ticks_msec()
		screen_position = p_screen_position
		strength = p_strength
		direction_deg = p_direction_deg
		lag_ms = p_lag_ms
		hand = p_hand

	## True when direction_deg carries a real angle.
	func has_direction() -> bool:
		return not is_nan(direction_deg)

	## +1 if the input went broadly up, -1 broadly down, 0 if sideways.
	##
	## Menus want this rather than a lane, because a lane is a gameplay idea
	## and the ring's lanes are deliberately offset so that nothing sits at the
	## top. "Up" and "down" as a person means them are 90-degree wedges around
	## straight up and straight down, which is what this reports.
	func vertical() -> int:
		if not has_direction():
			return 0
		if absf(angle_difference(deg_to_rad(direction_deg), deg_to_rad(90.0))) \
				< deg_to_rad(45.0):
			return 1
		if absf(angle_difference(deg_to_rad(direction_deg), deg_to_rad(270.0))) \
				< deg_to_rad(45.0):
			return -1
		return 0


func report_tap(source: String, screen_position: Vector2 = Vector2.ZERO, strength: float = 1.0) -> void:
	tap.emit(TapEvent.new(source, screen_position, strength))


## Report a tap that knows which way it went but not where it was.
##
## `angle_deg` is counter-clockwise from screen right, matching the game's
## sector_angle layout and _vec(). See ImuInput.gd for the conversion from the
## bridge's bearing convention. `lag_ms` is how long ago it really happened.
func report_direction(source: String, angle_deg: float, strength: float = 1.0,
		lag_ms: float = 0.0, hand: String = "") -> void:
	tap.emit(TapEvent.new(source, Vector2.ZERO, strength,
		fposmod(angle_deg, 360.0), lag_ms, hand))


## Called by gameplay once it knows whether a tap hit anything.
func report_judgement(source: String, hit: bool) -> void:
	tap_judged.emit(source, hit)
