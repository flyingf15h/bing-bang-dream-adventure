extends Control
## Diagnostic screen for the IMU link. Run this scene when flicks are not
## landing in the game and you need to know which half is at fault.
##
## It shows the same six lanes Gameplay.gd uses, lights the one each flick maps
## to, and prints the numbers behind the decision. Because it listens to
## TapInputBus rather than to ImuInput directly, a flick that lights a lane
## here has travelled the entire path the real game uses -- board, bridge,
## socket, bearing conversion, input bus. Only scoring is left out.
##
##   Godot editor:  open ImuTest.tscn and press F6
##   Command line:  godot --path . res://scenes/ImuTest.tscn
##
## With no board:  python dashboard/game_bridge.py --demo
## With a board:   python dashboard/game_bridge.py            (USB)
##                 python dashboard/game_bridge.py --host <ip>  (WiFi)

const TapEvent = preload("res://autoload/TapInputBus.gd").TapEvent

## Mirrors Gameplay.gd's default layout so this screen tests the same mapping.
const SECTOR_ANGLE := {1: 60.0, 2: 0.0, 3: 300.0, 4: 240.0, 5: 180.0, 6: 120.0}

const RADIUS := 190.0
const FLASH_DECAY := 2.5

var _flash := {}
var _log: Array[String] = []
var _font: Font


func _ready() -> void:
	_font = ThemeDB.fallback_font
	for sector in SECTOR_ANGLE:
		_flash[sector] = 0.0
	TapInputBus.tap.connect(_on_tap)
	ImuInput.link_changed.connect(_on_link_changed)
	_note("waiting -- start dashboard/game_bridge.py")
	set_process(true)


func _process(delta: float) -> void:
	for sector in _flash:
		_flash[sector] = maxf(0.0, _flash[sector] - delta * FLASH_DECAY)
	queue_redraw()


func _on_link_changed(up: bool) -> void:
	_note("link %s" % ("up" if up else "down"))


func _on_tap(event: TapEvent) -> void:
	var angle: float
	if event.has_direction():
		angle = event.direction_deg
	else:
		var offset: Vector2 = event.screen_position - _centre()
		if offset.length() < 12.0:
			return
		angle = fposmod(rad_to_deg(atan2(-offset.y, offset.x)), 360.0)

	var sector := _nearest_sector(angle)
	_flash[sector] = 1.0
	_note("%s  angle %6.1f deg  -> lane %d  strength %.2f"
		% [event.source, angle, sector, event.strength])


func _note(text: String) -> void:
	# Also to stdout, so this scene works as a command-line check:
	#   godot --headless --path . res://scenes/ImuTest.tscn
	# which is the form to use over SSH, or when the question is simply
	# "is anything arriving at all".
	print("[imutest] ", text)
	_log.push_front(text)
	if _log.size() > 12:
		_log.resize(12)


func _centre() -> Vector2:
	return Vector2(size.x * 0.5, size.y * 0.5 + 30.0)


func _nearest_sector(angle_deg: float) -> int:
	# Deliberately a copy of Gameplay.gd's version: this screen is here to
	# confirm that function's behaviour, so sharing an implementation would
	# mean a bug in it could hide from the very test meant to find it.
	var a := fposmod(angle_deg, 360.0)
	var best := 1
	var best_distance := 1e9
	for sector in SECTOR_ANGLE:
		var d: float = absf(float(SECTOR_ANGLE[sector]) - a)
		d = minf(d, 360.0 - d)
		if d < best_distance:
			best_distance = d
			best = int(sector)
	return best


func _vec(angle_deg: float) -> Vector2:
	var a := deg_to_rad(angle_deg)
	return Vector2(cos(a), -sin(a))


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, size), Color(0.07, 0.07, 0.11))

	var centre := _centre()
	draw_arc(centre, RADIUS, 0.0, TAU, 96, Color(1, 1, 1, 0.22), 2.0)

	for sector in SECTOR_ANGLE:
		var angle: float = float(SECTOR_ANGLE[sector])
		var at := centre + _vec(angle) * RADIUS
		var flash: float = _flash[sector]
		var colour := Color(0.45, 0.45, 0.6).lerp(Color(1.0, 0.85, 0.35), flash)
		draw_circle(at, 12.0 + 12.0 * flash, colour)
		draw_string(_font, at + Vector2(-5, 5), str(sector),
			HORIZONTAL_ALIGNMENT_LEFT, -1, 16, Color(0.05, 0.05, 0.08))

	# Where the board is being swung right now, at whatever length the swing
	# has reached against the flick threshold. Drawn dim and behind the flick
	# line: this one moves while you are still deciding, which is what makes a
	# wrong --front obvious without having to complete a gesture and guess
	# afterwards at which lane lit.
	if ImuInput.motion_supported and not is_nan(ImuInput.live_angle_deg):
		var threshold: float = maxf(1.0, ImuInput.flick_threshold_dps)
		var reach := clampf(ImuInput.live_swing_dps / threshold, 0.0, 1.0)
		if reach > 0.05:
			draw_line(centre,
				centre + _vec(ImuInput.live_angle_deg) * (RADIUS - 24.0) * reach,
				Color(0.55, 0.75, 1.0, 0.25 + 0.45 * reach), 8.0)

	# The last flick, drawn as the direction it actually came in at, so a
	# systematic rotation shows up as an arrow that never points at a lane.
	if not is_nan(ImuInput.last_bearing_deg):
		var angle := ImuInput.game_angle_of(ImuInput.last_bearing_deg)
		draw_line(centre, centre + _vec(angle) * (RADIUS - 24.0),
			Color(0.4, 0.9, 1.0), 3.0)

	draw_string(_font, Vector2(20, 30), ImuInput.debug_line(),
		HORIZONTAL_ALIGNMENT_LEFT, -1, 18, Color(0.8, 0.9, 1.0))
	draw_string(_font, Vector2(20, 54),
		"bridge: " + ImuInput.status_text,
		HORIZONTAL_ALIGNMENT_LEFT, -1, 14, Color(0.6, 0.65, 0.8))
	draw_string(_font, Vector2(20, 74),
		"click anywhere to compare a mouse tap against a flick",
		HORIZONTAL_ALIGNMENT_LEFT, -1, 13, Color(0.45, 0.5, 0.62))

	var y := size.y - 20.0 - _log.size() * 18.0
	for line in _log:
		draw_string(_font, Vector2(20, y), line,
			HORIZONTAL_ALIGNMENT_LEFT, -1, 14, Color(0.75, 0.78, 0.9))
		y += 18.0


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.pressed \
			and event.button_index == MOUSE_BUTTON_LEFT:
		TapInputBus.report_tap("mouse", event.position)
