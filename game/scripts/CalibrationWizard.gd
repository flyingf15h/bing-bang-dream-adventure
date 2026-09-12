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

## How long a game-level step (front axis, one direction-check flick) waits
## before giving the board back rather than holding it captured forever.
const CAPTURE_TIMEOUT_S := 25.0

var hand: String = ""

var _state: String = "intro"
var _heading: Label
var _body: Label
var _detail: RichTextLabel
var _progress: ProgressBar
var _buttons: HBoxContainer

## Direction check.
var _check_index: int = 0
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
	panel.custom_minimum_size = Vector2(560, 0)
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.06, 0.05, 0.11, 0.97)
	style.border_color = Color(0.55, 0.48, 0.85, 0.8)
	style.set_border_width_all(1)
	style.set_corner_radius_all(8)
	style.set_content_margin_all(20)
	panel.add_theme_stylebox_override("panel", style)
	centre.add_child(panel)

	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 10)
	panel.add_child(column)

	_heading = Label.new()
	_heading.add_theme_font_size_override("font_size", 20)
	column.add_child(_heading)

	_body = Label.new()
	_body.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_body.add_theme_font_size_override("font_size", 14)
	column.add_child(_body)

	_progress = ProgressBar.new()
	_progress.min_value = 0.0
	_progress.max_value = 1.0
	_progress.show_percentage = false
	_progress.custom_minimum_size = Vector2(0, 10)
	_progress.visible = false
	column.add_child(_progress)

	_detail = RichTextLabel.new()
	_detail.fit_content = true
	_detail.custom_minimum_size = Vector2(0, 0)
	_detail.add_theme_font_size_override("normal_font_size", 12)
	_detail.bbcode_enabled = true
	column.add_child(_detail)

	_buttons = HBoxContainer.new()
	_buttons.add_theme_constant_override("separation", 8)
	column.add_child(_buttons)


## `spec` is a list of [label, Callable] pairs, left to right.
func _set_buttons(spec: Array) -> void:
	for child in _buttons.get_children():
		child.queue_free()
	for pair in spec:
		var button := Button.new()
		button.text = String(pair[0])
		button.pressed.connect(pair[1])
		_buttons.add_child(button)


func _who() -> String:
	return ImuInput.hand_label(hand).capitalize()


# ----------------------------------------------------------------------
# Intro
# ----------------------------------------------------------------------
func _show_intro() -> void:
	_state = "intro"
	_heading.text = "Calibrate: %s" % _who()
	_body.text = ("Full measures everything -- gyro, accelerometer, "
		+ "magnetometer and mounting -- and takes a few minutes. Quick redoes "
		+ "only the mounting, for a board that moved to a new desk.")
	_progress.visible = false
	_detail.text = ""
	_set_buttons([
		["full calibration", func() -> void: _start_cal("full")],
		["quick", func() -> void: _start_cal("quick")],
		["cancel", queue_free],
	])


func _start_cal(kind: String) -> void:
	_state = "board_cal"
	_cal_running = true
	_heading.text = "Calibrate: %s" % _who()
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
	_heading.text = "Calibrate: %s -- %s" % [_who(), step.capitalize().replace("_", " ")]
	_body.text = String(record.get("prompt", ""))
	_progress.value = float(record.get("progress", 0.0))

	var quality: Dictionary = record.get("quality", {})
	_detail.text = _format_quality(step, quality)

	var buttons: Array = [["cancel", _cancel_board_cal]]
	if step == "orientation" and bool(quality.get("can_finish", false)):
		buttons.push_front(["continue", func() -> void: Settings.cal_advance(hand)])
	elif step == "check":
		buttons.push_front(["save", func() -> void: Settings.cal_save(hand)])
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
	_heading.text = "Calibrate: %s" % _who()
	_body.text = String(record.get("detail", ""))
	_detail.text = ""
	if accepted:
		_set_buttons([["continue", _show_front_axis], ["finish", _show_done]])
	else:
		_set_buttons([
			["redo", _show_intro],
			["continue anyway", _show_front_axis],
			["finish", _show_done],
		])


# ----------------------------------------------------------------------
# Front axis: which way the board's own axis points
# ----------------------------------------------------------------------
func _show_front_axis() -> void:
	_state = "front_axis"
	_heading.text = "Which way is which: %s" % _who()
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
		_set_buttons([["continue", _show_direction_check]])
		return
	_body.text = ("That flick looks like front %s (off by %.0f deg); this "
		+ "board is set to %s.") % [suggested, error, current]
	_set_buttons([
		["apply " + suggested, func() -> void:
			Settings.set_tuning("front", suggested)
			_show_direction_check()],
		["keep " + current, _show_direction_check],
	])


# ----------------------------------------------------------------------
# Direction check: four flicks solving rotation + mirror
# ----------------------------------------------------------------------
func _show_direction_check() -> void:
	_state = "direction_check"
	_check_index = 0
	_check_samples.clear()
	_progress.visible = true
	_progress.max_value = CHECK_DIRECTIONS.size()
	_detail.text = ""
	ImuInput.capture_only = true
	if not ImuInput.flick_received.is_connected(_on_check_flick):
		ImuInput.flick_received.connect(_on_check_flick)
	_prompt_check()


func _prompt_check() -> void:
	_heading.text = "Direction check: %s" % _who()
	var want: Array = CHECK_DIRECTIONS[_check_index]
	_body.text = "Flick %s (%d of %d), then let it come back." % [
		String(want[0]).to_upper(), _check_index + 1, CHECK_DIRECTIONS.size()]
	_progress.value = _check_index
	_set_buttons([["skip this check", _show_done], ["cancel", queue_free]])
	_capture_deadline = Time.get_ticks_msec() * 0.001 + CAPTURE_TIMEOUT_S


func _on_check_flick(record: Dictionary) -> void:
	if _state != "direction_check" or not record.has("bearing"):
		return
	if String(record.get("hand", "")) != hand:
		return       # the other board was flicked; not this check's business
	_capture_deadline = -1.0
	_check_samples.append({
		"name": String(CHECK_DIRECTIONS[_check_index][0]),
		"expect": float(CHECK_DIRECTIONS[_check_index][1]),
		"got": float(record["bearing"]),
	})
	_check_index += 1
	if _check_index < CHECK_DIRECTIONS.size():
		_prompt_check()
		return
	_finish_direction_check()


func _finish_direction_check() -> void:
	if ImuInput.flick_received.is_connected(_on_check_flick):
		ImuInput.flick_received.disconnect(_on_check_flick)
	var result := DirectionSolver.solve(_check_samples)
	_heading.text = "Direction check: %s" % _who()
	_progress.value = _progress.max_value
	_detail.text = "\n".join(result["lines"]) + "\n\n" + String(result["verdict"])
	_body.text = ""
	if bool(result["apply_worthy"]):
		_set_buttons([
			["apply", func() -> void:
				Settings.set_aim(float(result["offset"]), bool(result["flip"]), hand)
				_show_done()],
			["skip", _show_done],
		])
	else:
		_set_buttons([["continue", _show_done]])


# ----------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------
func _show_done() -> void:
	_state = "done"
	ImuInput.capture_only = false
	_heading.text = "All done: %s" % _who()
	_body.text = ("Calibration is stored on the board, and the mounting and "
		+ "aim correction are saved here. Run this again if the board moves "
		+ "to a new desk or a new mounting.")
	_progress.visible = false
	_detail.text = ""
	_set_buttons([["finish", queue_free]])
