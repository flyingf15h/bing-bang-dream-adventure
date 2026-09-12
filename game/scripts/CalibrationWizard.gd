extends Control
## Guided calibration for one board, opened from the Controllers tab.
##
## A thin renderer over bbda/calseq.py's CalSequence: every prompt and every
## decision about when a step is done comes from the cal_state/cal_done
## records Settings.cal_state_received/cal_done_received relay, authored on
## the bridge next to the thresholds that decide them. Nothing here judges
## whether a step passed -- if a rule is being decided in this file, it is in
## the wrong file. After the board-level sequence finishes, two more steps
## run entirely in the game, because they always have: which axis is front,
## and whether flicks land where they are aimed.
##
## Set `hand` before adding this to the tree (SettingsOverlay does). "" runs
## the single untagged board.

## The four square directions a learning/checking flick is aimed at, as
## bearings clockwise from up. Only these: they are the ones a person can
## throw accurately without thinking about it.
const CHECK_DIRECTIONS := [
	["up", 0.0], ["right", 90.0], ["down", 180.0], ["left", 270.0],
]

## How many times through all four directions the check asks for. Two rather
## than one: eight flicks average out a bad throw or a grip that loosened
## partway through far better than four do, and randomising the order each
## round (see _show_direction_check()) is what stops a steady drift over the
## session -- a hand tiring, say -- from reading as a rotation that is not
## really there.
const CHECK_ROUNDS := 2

## How long a game-level step (front axis, one direction-check flick) waits
## before giving the board back rather than holding it captured forever.
const CAPTURE_TIMEOUT_S := 25.0

## The three stages of the whole journey, for the "step X of Y" heading --
## purely a display concept, so a first-time player sees this as one thing
## with an end in sight rather than an open-ended sequence of screens.
const STAGE_NAMES := ["Board measurement", "Front axis", "Aim check"]

var hand: String = ""

var _state: String = "intro"
var _stage_label: Label
var _heading: Label
var _body: Label
var _detail: RichTextLabel
var _progress: ProgressBar
var _buttons: HBoxContainer

## Direction check.
var _check_index: int = 0
var _check_order: Array = []
var _check_samples: Array = []
var _capture_deadline: float = -1.0

## True once a board_cal sequence is known to be running, so cancelling the
## wizard also tells the bridge to stop rather than just closing the window
## on top of it.
var _cal_running: bool = false


func _ready() -> void:
	set_anchors_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_STOP    # sits over the game; eat clicks
	_build()
	Settings.cal_state_received.connect(_on_cal_state)
	Settings.cal_done_received.connect(_on_cal_done)
	Settings.front_suggested.connect(_on_front_suggested)
	set_process(true)
	_show_intro()


func _exit_tree() -> void:
	## Never leave the board captured or a sequence running behind a window
	## that just closed -- whichever way this wizard ends, cancelled, errored
	## or finished, both have to be undone or the board is dead to the game
	## with nothing on screen left to say why.
	ImuInput.capture_only = false
	if _cal_running:
		Settings.cal_cancel(hand)
		_cal_running = false


func _process(_delta: float) -> void:
	if _capture_deadline > 0.0 and Time.get_ticks_msec() * 0.001 > _capture_deadline:
		_capture_deadline = -1.0
		if _state == "front_axis":
			_body.text = "No flick registered -- try again, or skip this board for now."
			_set_buttons([["retry", _show_front_axis], ["skip", _show_direction_check],
				["close", queue_free]])
		elif _state == "direction_check":
			_body.text = "No flick registered -- try again, or finish without it."
			_set_buttons([["retry", _show_direction_check], ["finish", _show_done]])


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------
func _build() -> void:
	var scrim := ColorRect.new()
	scrim.color = Color(0, 0, 0, 0.55)
	scrim.set_anchors_preset(Control.PRESET_FULL_RECT)
	scrim.mouse_filter = Control.MOUSE_FILTER_STOP
	add_child(scrim)

	var centre := CenterContainer.new()
	centre.set_anchors_preset(Control.PRESET_FULL_RECT)
	centre.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(centre)

	var panel := PanelContainer.new()
	panel.custom_minimum_size = Vector2(640, 0)
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.06, 0.05, 0.11, 0.97)
	style.border_color = Color(0.55, 0.48, 0.85, 0.8)
	style.set_border_width_all(1)
	style.set_corner_radius_all(10)
	style.set_content_margin_all(28)
	panel.add_theme_stylebox_override("panel", style)
	centre.add_child(panel)

	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 12)
	panel.add_child(column)

	_stage_label = Label.new()
	_stage_label.add_theme_font_size_override("font_size", 14)
	_stage_label.modulate = Color(0.72, 0.66, 1.0)
	column.add_child(_stage_label)

	_heading = Label.new()
	_heading.add_theme_font_size_override("font_size", 26)
	column.add_child(_heading)

	_body = Label.new()
	_body.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_body.add_theme_font_size_override("font_size", 18)
	column.add_child(_body)

	_progress = ProgressBar.new()
	_progress.min_value = 0.0
	_progress.max_value = 1.0
	_progress.show_percentage = false
	_progress.custom_minimum_size = Vector2(0, 14)
	_progress.visible = false
	column.add_child(_progress)

	_detail = RichTextLabel.new()
	_detail.fit_content = true
	_detail.custom_minimum_size = Vector2(0, 0)
	_detail.add_theme_font_size_override("normal_font_size", 14)
	_detail.bbcode_enabled = true
	column.add_child(_detail)

	_buttons = HBoxContainer.new()
	_buttons.add_theme_constant_override("separation", 8)
	column.add_child(_buttons)


## `spec` is a list of [label, Callable] pairs, left to right, or
## [label, Callable, true] to mark one as the primary action -- larger, and
## the one a player's eye should land on first when a screen offers more
## than one way forward.
func _set_buttons(spec: Array) -> void:
	for child in _buttons.get_children():
		child.queue_free()
	for pair in spec:
		var button := Button.new()
		button.text = String(pair[0])
		button.pressed.connect(pair[1])
		if pair.size() > 2 and bool(pair[2]):
			button.add_theme_font_size_override("font_size", 18)
			button.custom_minimum_size = Vector2(0, 44)
		_buttons.add_child(button)


func _who() -> String:
	return ImuInput.hand_label(hand).capitalize()


func _set_stage(index: int) -> void:
	_stage_label.text = "Step %d of %d -- %s" % [
		index + 1, STAGE_NAMES.size(), STAGE_NAMES[index]]


# ----------------------------------------------------------------------
# Intro
# ----------------------------------------------------------------------
func _show_intro() -> void:
	_state = "intro"
	_stage_label.text = ""
	_heading.text = "Set up %s" % _who()
	_body.text = ("A few minutes, guided the whole way through: put it down, "
		+ "turn it onto each side, wave it around, then a couple of slides "
		+ "across the table. Nothing to get wrong -- just follow what's on "
		+ "screen, one thing at a time.")
	_progress.visible = false
	_detail.text = ""
	var start_full := func() -> void: _start_cal("full")
	_set_buttons([
		["start setup", start_full, true],
		["cancel", queue_free],
	])
	# Not everybody starting this has never calibrated before -- a board that
	# only moved to a new desk needs its mounting redone, not the physics
	# recaptured from scratch. Offered, but small and second: the common case
	# for anyone who found this screen at all is the first one.
	var quick := Button.new()
	quick.text = "already calibrated? quick re-aim instead"
	quick.flat = true
	quick.add_theme_font_size_override("font_size", 12)
	quick.pressed.connect(func() -> void: _start_cal("quick"))
	_buttons.add_child(quick)


func _start_cal(kind: String) -> void:
	_state = "board_cal"
	_cal_running = true
	_set_stage(0)
	_heading.text = "Set up %s" % _who()
	_body.text = "Starting..."
	_progress.visible = true
	_progress.value = 0.0
	_detail.text = ""
	_set_buttons([["cancel", _cancel_board_cal]])
	Settings.cal_start(hand, kind)


func _cancel_board_cal() -> void:
	Settings.cal_cancel(hand)
	_cal_running = false
	queue_free()


# ----------------------------------------------------------------------
# Board-level sequence: rest -> six_sides -> wave -> orientation -> check
# ----------------------------------------------------------------------
func _on_cal_state(record: Dictionary) -> void:
	if _state != "board_cal" or String(record.get("hand", "")) != hand:
		return
	var step := String(record.get("step", ""))
	_heading.text = "Set up %s -- %s" % [_who(), step.capitalize().replace("_", " ")]
	_body.text = String(record.get("prompt", ""))
	_progress.value = float(record.get("progress", 0.0))

	var quality: Dictionary = record.get("quality", {})
	_detail.text = _format_quality(step, quality)

	var buttons: Array = [["cancel", _cancel_board_cal]]
	if step == "orientation" and bool(quality.get("can_finish", false)):
		var advance := func() -> void: Settings.cal_advance(hand)
		buttons.push_front(["continue", advance, true])
	elif step == "check":
		var save := func() -> void: Settings.cal_save(hand)
		buttons.push_front(["save", save, true])
	_set_buttons(buttons)


func _format_quality(step: String, quality: Dictionary) -> String:
	match step:
		"six_sides":
			var done := 0
			var lines: PackedStringArray = []
			for face in quality:
				var mark := "[color=#a8ffb0]done[/color]" if bool(quality[face]) \
					else "[color=#888]waiting[/color]"
				if bool(quality[face]):
					done += 1
				lines.append("%s: %s" % [String(face), mark])
			return "\n".join(lines) + "\n\n%d of 6 sides" % done
		"orientation":
			var moves: Array = quality.get("moves_done", [])
			return ("up: %s\nslides: %s"
				% ["found" if bool(quality.get("up_found", false)) else "waiting",
				   ", ".join(moves) if not moves.is_empty() else "none yet"])
		"check":
			var lines: PackedStringArray = []
			for check in quality.get("checks", []):
				var mark := "[color=#a8ffb0]✓[/color]" if bool(check.get("ok", false)) \
					else "[color=#ffb0b0]✗[/color]"
				lines.append("%s  %s: %s" % [mark, String(check.get("name", "")),
					str(check.get("value", ""))])
			return "\n".join(lines)
		_:
			return ""


func _on_cal_done(record: Dictionary) -> void:
	if _state != "board_cal" or String(record.get("hand", "")) != hand:
		return
	_cal_running = false
	var accepted := bool(record.get("accepted", false))
	_progress.visible = false
	_heading.text = "Set up %s" % _who()
	_body.text = String(record.get("detail", ""))
	_detail.text = ""
	if accepted:
		_set_buttons([["continue", _show_front_axis, true], ["finish", _show_done]])
	else:
		_set_buttons([
			["redo", _show_intro],
			["continue anyway", _show_front_axis, true],
			["finish", _show_done],
		])


# ----------------------------------------------------------------------
# Front axis: which way the board's own axis points
# ----------------------------------------------------------------------
func _show_front_axis() -> void:
	_state = "front_axis"
	_set_stage(1)
	_heading.text = "Set up %s" % _who()
	_body.text = ("Hold the board the way you play, then flick it straight "
		+ "up and let it settle.")
	_progress.visible = false
	_detail.text = ""
	_set_buttons([["skip", _show_direction_check], ["cancel", queue_free]])
	ImuInput.capture_only = true
	_capture_deadline = Time.get_ticks_msec() * 0.001 + CAPTURE_TIMEOUT_S
	Settings.learn_front(0.0, hand)


func _on_front_suggested(record: Dictionary) -> void:
	if _state != "front_axis" or String(record.get("hand", "")) != hand:
		return
	_capture_deadline = -1.0
	var suggested := String(record.get("front", ""))
	var current := String(record.get("current", ""))
	var error := float(record.get("error_deg", 0.0))
	if suggested == current:
		_body.text = ("That flick fits the current axis %s to within %.0f "
			+ "deg -- nothing to change.") % [current, error]
		_set_buttons([["continue", _show_direction_check, true]])
		return
	_body.text = ("That flick looks like front %s (off by %.0f deg); this "
		+ "board is set to %s.") % [suggested, error, current]
	var apply_front := func() -> void:
		Settings.set_tuning("front", suggested)
		_show_direction_check()
	_set_buttons([
		["apply " + suggested, apply_front, true],
		["keep " + current, _show_direction_check],
	])


# ----------------------------------------------------------------------
# Direction check: several flicks solving rotation + mirror
# ----------------------------------------------------------------------
func _show_direction_check() -> void:
	_state = "direction_check"
	_set_stage(2)
	_check_index = 0
	_check_samples.clear()
	# All four directions, CHECK_ROUNDS times, each round in its own random
	# order -- so a hand tiring over the session reads as noise the fit
	# averages out, not as a rotation that is not really there.
	_check_order.clear()
	for _round in range(CHECK_ROUNDS):
		var order: Array = range(CHECK_DIRECTIONS.size())
		order.shuffle()
		_check_order.append_array(order)
	_progress.visible = true
	_progress.max_value = _check_order.size()
	_detail.text = ""
	ImuInput.capture_only = true
	if not ImuInput.flick_received.is_connected(_on_check_flick):
		ImuInput.flick_received.connect(_on_check_flick)
	_prompt_check()


func _prompt_check() -> void:
	_heading.text = "Set up %s" % _who()
	var want: Array = CHECK_DIRECTIONS[_check_order[_check_index]]
	_body.text = "Flick %s (%d of %d), then let it come back." % [
		String(want[0]).to_upper(), _check_index + 1, _check_order.size()]
	_progress.value = _check_index
	_set_buttons([["skip this check", _show_done], ["cancel", queue_free]])
	_capture_deadline = Time.get_ticks_msec() * 0.001 + CAPTURE_TIMEOUT_S


func _on_check_flick(record: Dictionary) -> void:
	if _state != "direction_check" or not record.has("bearing"):
		return
	if String(record.get("hand", "")) != hand:
		return       # the other board was flicked; not this check's business
	_capture_deadline = -1.0
	var want: Array = CHECK_DIRECTIONS[_check_order[_check_index]]
	_check_samples.append({
		"name": String(want[0]),
		"expect": float(want[1]),
		"got": float(record["bearing"]),
	})
	_check_index += 1
	if _check_index < _check_order.size():
		_prompt_check()
		return
	_finish_direction_check()


func _finish_direction_check() -> void:
	if ImuInput.flick_received.is_connected(_on_check_flick):
		ImuInput.flick_received.disconnect(_on_check_flick)
	var result := DirectionSolver.solve(_check_samples)
	_heading.text = "Set up %s" % _who()
	_progress.value = _progress.max_value
	_detail.text = "\n".join(result["lines"]) + "\n\n" + String(result["verdict"])
	_body.text = ""
	if not bool(result["consistent"]):
		# Too inconsistent to trust any single correction from -- offering to
		# accept it anyway would be worse than asking again, so "try again"
		# is the one way forward rather than one choice among several.
		_set_buttons([
			["try again", _show_direction_check, true],
			["skip for now", _show_done],
		])
		return
	if bool(result["apply_worthy"]):
		var apply_aim := func() -> void:
			Settings.set_aim(float(result["offset"]), bool(result["flip"]), hand)
			_show_done()
		_set_buttons([
			["apply", apply_aim, true],
			["skip", _show_done],
		])
	else:
		_set_buttons([["continue", _show_done, true]])


# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------
func _show_done() -> void:
	_state = "done"
	ImuInput.capture_only = false
	_stage_label.text = ""
	_heading.text = "All done: %s" % _who()
	_body.text = ("Calibration is stored on the board, and the mounting and "
		+ "aim correction are saved here. Run this again if the board moves "
		+ "to a new desk or a new mounting.")
	_progress.visible = false
	_detail.text = ""
	_set_buttons([["finish", queue_free, true]])
