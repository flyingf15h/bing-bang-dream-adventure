extends CanvasLayer
## The IMU debug panel: a checkbox, and everything behind it.
##
## Add it to any scene -- `add_child(preload("res://ImuDebugPanel.gd").new())`
## -- and it brings its own checkbox, its own layout and its own connections.
## It is built in code rather than as a .tscn for that reason: a scene would
## have to be instanced and wired up per screen, and this has to be available
## on the title screen and in the middle of a song without either of them
## knowing anything about it.
##
## What it is for
## --------------
## Two questions cannot be answered by playing: "which way does the board think
## it is pointing" and "is a flick that did not register too weak, or aimed
## wrong". Both are answerable in seconds with the right numbers on screen, and
## essentially unanswerable without them -- which is why tuning an IMU without
## a panel like this turns into changing a flag and replaying a song.
##
## Nothing here is computed locally. Detection lives in the bridge, so every
## control sends its change there and then displays what the bridge reports
## back. A slider that moved but did not take effect will not look like it did.

const FRONTS: PackedStringArray = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]

## Directions to aim a learning flick at, as bearings clockwise from up. Only
## the four square ones: they are the ones a person can make accurately without
## thinking about it, which is the whole requirement for a reference gesture.
const LEARN_DIRECTIONS := [
	["up", 0.0], ["right", 90.0], ["down", 180.0], ["left", 270.0],
]

## How long after a slider moves before the bridge is told. Long enough that a
## drag is one message rather than sixty, short enough to feel immediate.
const PUSH_DELAY := 0.25

## Pixels of list per wheel notch.
const WHEEL_STEP := 56

var _check: CheckButton
var _panel: PanelContainer
var _rows: Dictionary = {}          # name -> Label, for the live readouts
var _front_picker: OptionButton
var _learn_picker: OptionButton
var _learn_result: Label
var _learn_apply: Button
var _suggested_front: String = ""
var _rest_result: Label
var _bias_result: Label
var _file_note: Label
var _sliders: Dictionary = {}
var _dialog: FileDialog
var _scroll: ScrollContainer

var _push_in: float = -1.0
var _lane_hits: int = 0
var _lane_misses: int = 0

## --- the direction check --------------------------------------------------
## Which direction it is waiting for (-1 when idle), what it has collected so
## far, and the correction it worked out but has not applied yet.
var _check_step: int = -1
var _check_samples: Array = []
var _check_offset: float = 0.0
var _check_flip: bool = false
var _check_prompt: Label
var _check_result: Label
var _check_apply: Button
var _check_start: Button
## When to give up waiting for a flick. A check left half-finished would hold
## the board out of the input bus for ever, which looks exactly like the board
## having died.
var _capture_deadline: float = -1.0


func _ready() -> void:
	layer = 100
	_build()
	_check.button_pressed = ImuSettings.panel_open
	_panel.visible = ImuSettings.panel_open

	ImuSettings.changed.connect(_refresh_controls)
	ImuSettings.front_suggested.connect(_on_front_suggested)
	ImuSettings.rest_measured.connect(_on_rest_measured)
	ImuSettings.bias_written.connect(_on_bias_written)
	TapInputBus.tap_judged.connect(func(source: String, hit: bool) -> void:
		if source != "imu":
			return
		if hit:
			_lane_hits += 1
		else:
			_lane_misses += 1)

	# Straight from ImuInput rather than from the input bus: during a check the
	# flicks are deliberately kept off the bus, and these are the only place
	# they still appear.
	ImuInput.flick_received.connect(_on_check_flick)

	_refresh_controls()
	ImuSettings.request_config()
	set_process(true)


func _exit_tree() -> void:
	## Never leave the board captured. This node dies on every scene change,
	## and a check running when that happens would otherwise take the input bus
	## with it -- the board would stop playing and nothing would say why.
	ImuInput.capture_only = false


func _input(event: InputEvent) -> void:
	## Take the wheel before any control can see it, and scroll the list with it.
	##
	## Done here rather than left to the containers because the default
	## behaviour is genuinely dangerous in this panel: a slider under the
	## pointer treats a wheel notch as an edit, so running the list past the
	## sensitivity section retunes the board -- silently, and applied to the
	## bridge before there is any way to notice. The controls are also set
	## non-scrollable, but this is the part that does not depend on getting
	## Godot's mouse filters exactly right on every container in the tree.
	if not _panel.visible or not (event is InputEventMouseButton):
		return
	if not event.pressed:
		return
	var step := 0
	if event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
		step = 1
	elif event.button_index == MOUSE_BUTTON_WHEEL_UP:
		step = -1
	if step == 0 or not _panel.get_global_rect().has_point(event.position):
		return
	_scroll.scroll_vertical += step * WHEEL_STEP
	get_viewport().set_input_as_handled()


func _process(delta: float) -> void:
	if _capture_deadline > 0.0 and Time.get_ticks_msec() * 0.001 > _capture_deadline:
		_end_check("gave up waiting for a flick -- the board is playable again")
	if _push_in > 0.0:
		_push_in -= delta
		if _push_in <= 0.0:
			ImuSettings.push_to_bridge()
			ImuSettings.save_settings()
	if _panel.visible:
		_refresh_readouts()


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------
func _build() -> void:
	var root := Control.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(root)

	_check = CheckButton.new()
	_check.text = "IMU debug"
	_check.set_anchors_preset(Control.PRESET_TOP_RIGHT)
	_check.offset_left = -170.0
	_check.offset_top = 84.0
	_check.offset_right = -12.0
	_check.offset_bottom = 116.0
	_check.modulate = Color(1, 1, 1, 0.65)
	_check.toggled.connect(_on_toggled)
	root.add_child(_check)

	_panel = PanelContainer.new()
	# Pinned to the right edge and to both top and bottom, so the height it can
	# use is the window's rather than a number guessed here. The scroll
	# container inside takes care of the rest.
	_panel.anchor_left = 1.0
	_panel.anchor_top = 0.0
	_panel.anchor_right = 1.0
	_panel.anchor_bottom = 1.0
	_panel.offset_left = -430.0
	_panel.offset_top = 120.0
	_panel.offset_right = -12.0
	_panel.offset_bottom = -12.0
	# Grows left, not right. A container cannot be smaller than its contents,
	# so on a narrow window this would otherwise widen itself off the edge of
	# the screen -- taking the scrollbar, and half the values, with it.
	_panel.grow_horizontal = Control.GROW_DIRECTION_BEGIN
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.05, 0.04, 0.09, 0.94)
	style.border_color = Color(0.55, 0.48, 0.85, 0.7)
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.set_content_margin_all(10)
	_panel.add_theme_stylebox_override("panel", style)
	root.add_child(_panel)

	_scroll = ScrollContainer.new()
	var scroll := _scroll     # local alias, for readability below
	# Only a width is asked for. A minimum height would set a floor the panel
	# could not go below, which on a short window is the same overflow problem
	# in the other direction.
	scroll.custom_minimum_size = Vector2(384, 0)
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_panel.add_child(scroll)

	var column := VBoxContainer.new()
	column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	column.add_theme_constant_override("separation", 4)
	# See _new_row(): the column would otherwise eat every wheel event that
	# lands between its children, which is why the list refused to scroll.
	column.mouse_filter = Control.MOUSE_FILTER_IGNORE
	scroll.add_child(column)

	_build_link(column)
	_build_live(column)
	_build_orientation(column)
	_build_direction_check(column)
	_build_sensitivity(column)
	_build_assist(column)
	_build_accuracy(column)
	_build_display(column)
	_build_storage(column)

	_dialog = FileDialog.new()
	_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_dialog.add_filter("*.json", "IMU settings")
	_dialog.size = Vector2i(760, 520)
	root.add_child(_dialog)


func _build_link(column: VBoxContainer) -> void:
	_heading(column, "Link")
	_readout(column, "bridge", "bridge")
	_readout(column, "board", "board")
	_readout(column, "counts", "flicks")
	_readout(column, "refusal", "last refusal")


func _build_live(column: VBoxContainer) -> void:
	_heading(column, "Live")
	_readout(column, "bearing", "pointing")
	# A bar rather than a number, because the useful question is not "how many
	# dps" but "how close to counting", and that is a distance to a line.
	var bar := ProgressBar.new()
	bar.max_value = 150.0
	bar.show_percentage = false
	bar.custom_minimum_size = Vector2(0, 14)
	column.add_child(bar)
	_rows["swing_bar"] = bar
	_readout(column, "swing", "swing")
	_readout(column, "flick", "last flick")


func _build_orientation(column: VBoxContainer) -> void:
	_heading(column, "Orientation")
	_note(column, "Which board axis points away from you. Wrong here and "
		+ "flicks land in the wrong lane, or read as rolls and are refused.")

	var row := _new_row(column)
	var label := Label.new()
	label.text = "front axis"
	label.custom_minimum_size = Vector2(120, 0)
	row.add_child(label)
	_front_picker = OptionButton.new()
	for choice in FRONTS:
		_front_picker.add_item(choice)
	_front_picker.item_selected.connect(func(index: int) -> void:
		ImuSettings.set_tuning("front", FRONTS[index])
		_queue_push())
	row.add_child(_front_picker)

	_note(column, "Or let it work the axis out: pick a direction, press the "
		+ "button, then flick that way once.")
	var learn_row := _new_row(column)
	_learn_picker = OptionButton.new()
	for entry in LEARN_DIRECTIONS:
		_learn_picker.add_item("flick " + String(entry[0]))
	learn_row.add_child(_learn_picker)
	var learn_button := Button.new()
	learn_button.text = "learn from my next flick"
	learn_button.pressed.connect(func() -> void:
		var chosen: Array = LEARN_DIRECTIONS[_learn_picker.selected]
		_learn_result.text = "waiting for a flick %s..." % chosen[0]
		_learn_apply.visible = false
		ImuSettings.learn_front(float(chosen[1])))
	learn_row.add_child(learn_button)

	_learn_result = Label.new()
	_learn_result.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_learn_result.add_theme_font_size_override("font_size", 12)
	column.add_child(_learn_result)

	_learn_apply = Button.new()
	_learn_apply.text = "apply"
	_learn_apply.visible = false
	_learn_apply.pressed.connect(func() -> void:
		if _suggested_front == "":
			return
		ImuSettings.set_tuning("front", _suggested_front)
		_learn_apply.visible = false
		_queue_push()
		_refresh_controls())
	column.add_child(_learn_apply)

	_slider(column, "sector_offset_deg", "lane offset", 0.0, 60.0, 1.0)


## How long a check waits for each flick before giving the board back.
const CHECK_TIMEOUT_S := 25.0

## Worst per-flick disagreement, in degrees, still called a consistent answer.
## An eighth of a turn is roughly what a hand throwing four flicks in a hurry
## produces; past a quarter they are no longer describing one mapping at all.
const CHECK_TIGHT_DEG := 20.0
const CHECK_LOOSE_DEG := 45.0

## Rotation small enough not to be worth correcting. Well inside a lane, and
## inside what a person can aim by hand, so "fixing" it would be fitting the
## correction to the throw rather than to the board.
const CHECK_NEGLIGIBLE_DEG := 8.0


func _build_direction_check(column: VBoxContainer) -> void:
	_heading(column, "Direction check")
	_note(column, "Whether flicks go where you aim them, measured rather than "
		+ "guessed. Four flicks, one each way. Nothing is scored from them and "
		+ "the board will not start or play anything while it is running.")

	var row := _new_row(column)
	_check_start = Button.new()
	_check_start.text = "start check"
	_check_start.pressed.connect(_start_check)
	row.add_child(_check_start)
	var cancel := Button.new()
	cancel.text = "cancel"
	cancel.pressed.connect(func() -> void:
		if _check_step >= 0:
			_end_check("cancelled"))
	row.add_child(cancel)
	var clear := Button.new()
	clear.text = "clear correction"
	clear.pressed.connect(func() -> void:
		ImuSettings.clear_aim()
		_check_result.text = ("correction cleared -- bearings are now used exactly "
			+ "as the board reports them"))
	row.add_child(clear)

	_check_prompt = Label.new()
	_check_prompt.add_theme_font_size_override("font_size", 15)
	_check_prompt.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(_check_prompt)

	_check_result = Label.new()
	_check_result.add_theme_font_size_override("font_size", 12)
	_check_result.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(_check_result)

	_check_apply = Button.new()
	_check_apply.text = "apply this correction"
	_check_apply.visible = false
	_check_apply.pressed.connect(func() -> void:
		ImuSettings.set_aim(_check_offset, _check_flip)
		_check_apply.visible = false
		_check_result.text = ("applied. Run the check again to confirm it now "
			+ "reads straight."))
	column.add_child(_check_apply)

	_readout(column, "aim", "correction")


func _start_check() -> void:
	_check_step = 0
	_check_samples.clear()
	_check_apply.visible = false
	_check_result.text = ""
	# The board stops being a controller for the duration. Without this the very
	# first flick would do whatever the screen behind the panel does with one --
	# on the title screen that is "start the game", which ends the check by
	# navigating away from the panel running it.
	ImuInput.capture_only = true
	_prompt_check()


func _prompt_check() -> void:
	_capture_deadline = Time.get_ticks_msec() * 0.001 + CHECK_TIMEOUT_S
	var want: Array = LEARN_DIRECTIONS[_check_step]
	_check_prompt.text = "flick %s  (%d of %d)" % [
		String(want[0]).to_upper(), _check_step + 1, LEARN_DIRECTIONS.size()]


func _end_check(why: String) -> void:
	_check_step = -1
	_capture_deadline = -1.0
	ImuInput.capture_only = false
	_check_prompt.text = ""
	if why != "":
		_check_result.text = why


func _on_check_flick(record: Dictionary) -> void:
	if _check_step < 0 or not record.has("bearing"):
		return
	_check_samples.append({
		"name": String(LEARN_DIRECTIONS[_check_step][0]),
		"expect": float(LEARN_DIRECTIONS[_check_step][1]),
		# The raw bearing, before whatever correction is already in force. The
		# check solves for the whole correction from scratch, so that running it
		# twice confirms an answer instead of stacking a second one on top.
		"got": float(record["bearing"]),
	})
	_check_step += 1
	if _check_step < LEARN_DIRECTIONS.size():
		_prompt_check()
		return
	_finish_check()


## Fit one correction to every flick collected: which single rotation, with or
## without a mirror, best explains all four at once.
##
## `spread` is the worst any one flick disagrees with that fit, and it is the
## number that decides whether the answer means anything. A tight spread with a
## large offset is a board held at an angle -- fixable, and exactly what this
## screen is for. A wide spread is four flicks that do not describe one mapping
## at all, and no offset would fix it: that is a wrong front axis, or four
## flicks thrown too lazily to have a direction.
func _fit_check(flip: bool) -> Dictionary:
	# Averaged as vectors rather than as numbers, because these are angles: the
	# mean of 350 and 10 is 0, and arithmetic makes it 180.
	var sx: float = 0.0
	var sy: float = 0.0
	for sample in _check_samples:
		var error: float = _sample_error(sample, flip, 0.0)
		sx += cos(error)
		sy += sin(error)
	var mean: float = atan2(sy, sx)
	var spread: float = 0.0
	for sample in _check_samples:
		spread = maxf(spread, absf(angle_difference(
			mean, _sample_error(sample, flip, 0.0))))
	return {
		"offset": fposmod(rad_to_deg(mean), 360.0),
		"spread": rad_to_deg(spread),
	}


## How far a flick landed from where it was aimed, in radians, once `flip` and
## `offset_deg` have been applied to it. Zero means the correction under test
## puts that flick exactly where the player said they were throwing it.
func _sample_error(sample: Dictionary, flip: bool, offset_deg: float) -> float:
	var measured: float = float(sample["got"])
	if flip:
		measured = -measured
	return angle_difference(deg_to_rad(measured + offset_deg),
		deg_to_rad(float(sample["expect"])))


func _finish_check() -> void:
	var direct: Dictionary = _fit_check(false)
	var mirrored: Dictionary = _fit_check(true)
	# The mirror has to explain the flicks *better*, not merely explain them. A
	# rotation is the ordinary fault and a reflection is the surprising one, so
	# it has to earn being named -- and with four flicks the two fits are never
	# far apart by chance.
	var best: Dictionary = direct
	_check_flip = false
	if float(mirrored["spread"]) < float(direct["spread"]) - 5.0:
		best = mirrored
		_check_flip = true
	_check_offset = float(best["offset"])
	var spread: float = float(best["spread"])
	# Signed, so it can be said as a direction rather than as a number.
	var turn: float = rad_to_deg(angle_difference(0.0, deg_to_rad(_check_offset)))

	var lines: PackedStringArray = []
	for sample in _check_samples:
		lines.append("%s: aimed %.0f, read %.0f  (%+.0f off)" % [
			String(sample["name"]), float(sample["expect"]),
			float(sample["got"]),
			-rad_to_deg(_sample_error(sample, false, 0.0))])

	var verdict: String = ""
	if spread > CHECK_LOOSE_DEG:
		verdict = ("These four do not agree with each other -- one is %.0f deg "
			+ "from the best fit -- so no single correction can fix them. That "
			+ "is almost always the front axis: use \"learn from my next "
			+ "flick\" above, then run this again. If it persists, throw them "
			+ "harder; a lazy flick has no clear direction to read.") % spread
		_check_apply.visible = false
	elif _check_flip:
		verdict = ("Left and right are mirrored, and the ring is turned %.0f "
			+ "deg on top of that. A rotation alone cannot undo a mirror, "
			+ "which is exactly why this is worth measuring.") % absf(turn)
		_check_apply.visible = true
	elif absf(turn) <= CHECK_NEGLIGIBLE_DEG:
		verdict = ("Directions are right: off by %.0f deg, which is inside what "
			+ "a hand can aim. Nothing to fix.") % absf(turn)
		_check_apply.visible = false
	else:
		verdict = ("Flicks land %.0f deg %s of where you aim them, and do it "
			+ "consistently. Applying this turns every bearing back.") % [
			absf(turn), "anticlockwise" if turn > 0.0 else "clockwise"]
		_check_apply.visible = true
	if spread > CHECK_TIGHT_DEG and spread <= CHECK_LOOSE_DEG:
		verdict += (" The four disagree by up to %.0f deg, so this is a rough "
			+ "fit -- worth running once more.") % spread

	_check_result.text = "\n".join(lines) + "\n\n" + verdict
	_end_check("")


func _build_sensitivity(column: VBoxContainer) -> void:
	_heading(column, "Sensitivity")
	_note(column, "How hard a movement has to be, and how clean, before it "
		+ "counts as a flick.")
	_slider(column, "on_threshold_dps", "flick threshold", 40.0, 500.0, 5.0)
	_slider(column, "min_swing", "swing floor", 0.05, 0.95, 0.05)
	_slider(column, "min_margin", "lane margin", 0.0, 0.4, 0.01)
	_slider(column, "refractory_ms", "refractory", 60.0, 500.0, 10.0)
	_note(column, "These start almost all the way down: reaching the threshold "
		+ "is very nearly the whole test, and the swing floor only still "
		+ "rejects a movement that is essentially a roll, which has no "
		+ "direction to report. Raise the threshold if stray movements "
		+ "register, and the refractory if the return stroke fires a second "
		+ "flick the opposite way.")

	_heading(column, "Latency")
	_note(column, "A flick cannot be named until enough of it has happened. "
		+ "This is how much of it is enough.")
	_slider(column, "commit_fraction", "report at", 0.2, 0.9, 0.05)
	_note(column, "The bridge stops measuring once the rotation has fallen to "
		+ "this fraction of its own peak, and sends the flick then. Higher "
		+ "reports sooner off less of the movement; lower waits for the whole "
		+ "swing to die away, which is where this used to sit and is worth "
		+ "about twice the delay. It does not change scoring -- every flick "
		+ "carries how late it was and the game reaches back by exactly that "
		+ "-- so what this moves is how quickly the screen answers you.")


func _build_assist(column: VBoxContainer) -> void:
	_heading(column, "Leniency")
	_note(column, "How generously a flick that did register is matched to a "
		+ "note. Nothing here is sent to the bridge -- it is scoring, and only "
		+ "the game knows where the notes are.")
	_slider(column, "lane_tolerance_deg", "aim tolerance", 30.0, 100.0, 1.0, false)
	_slider(column, "timing_scale", "window stretch", 1.0, 4.0, 0.05, false)
	_note(column, "Aim tolerance is how far off a lane a flick may point and "
		+ "still reach the note in it; 30 is strict, so only ever the nearest "
		+ "lane, and the default 75 reaches past it either side. Window "
		+ "stretch multiplies the hit windows for flicks alone -- keys and "
		+ "clicks are judged the same as always. If flicks are landing in the "
		+ "wrong lane rather than merely missing, the direction check above is "
		+ "the fix; leniency only widens what a correct direction can reach.")


func _build_accuracy(column: VBoxContainer) -> void:
	_heading(column, "Accuracy")
	_note(column, "Bias is what the gyro reads while the board is still. It "
		+ "never stops, so it is what makes a resting board look like it is "
		+ "creeping.")

	var row := _new_row(column)
	var measure := Button.new()
	measure.text = "measure (2s, hold still)"
	measure.pressed.connect(func() -> void:
		_rest_result.text = "measuring -- put the board down..."
		ImuSettings.measure_rest(2.0))
	row.add_child(measure)
	var write := Button.new()
	write.text = "write to board"
	write.pressed.connect(func() -> void:
		_bias_result.text = "writing..."
		ImuSettings.write_bias())
	row.add_child(write)

	_rest_result = Label.new()
	_rest_result.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_rest_result.add_theme_font_size_override("font_size", 12)
	column.add_child(_rest_result)
	_bias_result = Label.new()
	_bias_result.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bias_result.add_theme_font_size_override("font_size", 12)
	column.add_child(_bias_result)
	_readout(column, "stored_bias", "board bias")

	var calibrated := CheckBox.new()
	calibrated.text = "apply the board's stored calibration"
	calibrated.button_pressed = ImuSettings.calibrated
	calibrated.toggled.connect(func(on: bool) -> void:
		ImuSettings.set_tuning("calibrated", on)
		_queue_push())
	column.add_child(calibrated)
	_rows["calibrated_box"] = calibrated


func _build_display(column: VBoxContainer) -> void:
	_heading(column, "Display")
	var arrow := CheckBox.new()
	arrow.text = "show the arrow  (I)"
	arrow.button_pressed = ImuSettings.show_arrow
	arrow.toggled.connect(func(on: bool) -> void:
		ImuSettings.show_arrow = on
		ImuSettings.save_settings()
		ImuSettings.changed.emit())
	column.add_child(arrow)
	_rows["arrow_box"] = arrow

	var only_hits := CheckBox.new()
	only_hits.text = "colour only registered hits"
	only_hits.button_pressed = ImuSettings.colour_only_hits
	only_hits.toggled.connect(func(on: bool) -> void:
		ImuSettings.colour_only_hits = on
		ImuSettings.save_settings()
		ImuSettings.changed.emit())
	column.add_child(only_hits)
	_rows["hits_box"] = only_hits
	_note(column, "With this on, colour means one thing only: that flick hit "
		+ "a note. Everything else -- the live arrow, refusals, flicks that "
		+ "hit nothing -- stays grey.")

	var only_arrows := CheckBox.new()
	only_arrows.text = "draw detected flicks only  (O)"
	only_arrows.button_pressed = ImuSettings.arrow_flicks_only
	only_arrows.toggled.connect(func(on: bool) -> void:
		ImuSettings.arrow_flicks_only = on
		ImuSettings.save_settings()
		ImuSettings.changed.emit())
	column.add_child(only_arrows)
	_rows["only_arrows_box"] = only_arrows
	_note(column, "A different question from the rule above. That one is about "
		+ "scoring; this is about detection -- with it on, the ring stays empty "
		+ "until a swing is strong and clean enough to be sent as a flick, and "
		+ "then shows that flick whether or not there was a note there. No "
		+ "live arrow following the board, no rest dot, no refusal mark.")
	_readout(column, "hit_rate", "flicks on notes")


func _build_storage(column: VBoxContainer) -> void:
	_heading(column, "Settings file")
	var row := _new_row(column)

	var save := Button.new()
	save.text = "save"
	save.pressed.connect(func() -> void:
		ImuSettings.save_settings()
		_file_note.text = "saved to " + ImuSettings.save_location())
	row.add_child(save)

	var export_button := Button.new()
	export_button.text = "export..."
	export_button.pressed.connect(_on_export_pressed)
	row.add_child(export_button)

	var import_button := Button.new()
	import_button.text = "import..."
	import_button.pressed.connect(_on_import_pressed)
	row.add_child(import_button)

	var defaults := Button.new()
	defaults.text = "defaults"
	defaults.pressed.connect(func() -> void:
		ImuSettings.reset_to_defaults()
		_file_note.text = "back to the defaults")
	row.add_child(defaults)

	_file_note = Label.new()
	_file_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_file_note.add_theme_font_size_override("font_size", 11)
	_file_note.text = "saved automatically to " + ImuSettings.save_location()
	column.add_child(_file_note)


# ----------------------------------------------------------------------
# Small builders
# ----------------------------------------------------------------------
func _new_row(parent: Node) -> HBoxContainer:
	## A row that does not swallow the mouse wheel.
	##
	## Containers inherit Control's default of MOUSE_FILTER_STOP, so a plain
	## HBoxContainer consumes wheel events that land on it and the scroll
	## container behind never sees them -- which is most of the panel's area,
	## since it is the gaps between and around the controls. Ignoring the mouse
	## here costs nothing: the controls inside still receive their own events.
	var row := HBoxContainer.new()
	row.mouse_filter = Control.MOUSE_FILTER_IGNORE
	parent.add_child(row)
	return row


func _heading(column: VBoxContainer, text: String) -> void:
	var spacer := Control.new()
	spacer.custom_minimum_size = Vector2(0, 8)
	column.add_child(spacer)
	var label := Label.new()
	label.text = text.to_upper()
	label.add_theme_font_size_override("font_size", 12)
	label.modulate = Color(0.72, 0.66, 1.0)
	column.add_child(label)


func _note(column: VBoxContainer, text: String) -> void:
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.add_theme_font_size_override("font_size", 11)
	label.modulate = Color(1, 1, 1, 0.45)
	column.add_child(label)


func _readout(column: VBoxContainer, key: String, caption: String) -> void:
	var row := _new_row(column)
	var name_label := Label.new()
	name_label.text = caption
	name_label.custom_minimum_size = Vector2(120, 0)
	name_label.add_theme_font_size_override("font_size", 12)
	name_label.modulate = Color(1, 1, 1, 0.5)
	row.add_child(name_label)
	var value := Label.new()
	value.add_theme_font_size_override("font_size", 12)
	value.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	value.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(value)
	_rows[key] = value


## A labelled slider over one setting.
##
## `to_bridge` is the difference between the two kinds of setting this panel
## edits: detection values go to the bridge and are only believed once it
## echoes them back, while the game's own -- the assist values -- take effect
## the moment the slider moves and are saved here.
func _slider(column: VBoxContainer, key: String, caption: String,
		low: float, high: float, step: float, to_bridge: bool = true) -> void:
	var row := _new_row(column)
	var name_label := Label.new()
	name_label.text = caption
	name_label.custom_minimum_size = Vector2(110, 0)
	name_label.add_theme_font_size_override("font_size", 12)
	row.add_child(name_label)

	var slider := HSlider.new()
	slider.min_value = low
	slider.max_value = high
	slider.step = step
	# The wheel scrolls the panel, it does not edit whatever happens to be
	# under the pointer. A slider that changes on scroll means running the list
	# past a control silently retunes the board, and the change is applied
	# before there is any way to notice it happened.
	slider.scrollable = false
	# ...and PASS so the wheel carries on to the ScrollContainer behind it.
	# Without this the slider merely swallows the event: safe, but the list
	# refuses to scroll wherever the pointer happens to be over a control,
	# which feels broken in a different way. Dragging still works, because the
	# slider accepts button events and only declines the wheel.
	slider.mouse_filter = Control.MOUSE_FILTER_PASS
	slider.value = ImuSettings.get(key)
	slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	slider.custom_minimum_size = Vector2(150, 0)
	row.add_child(slider)

	var value := Label.new()
	value.custom_minimum_size = Vector2(56, 0)
	value.add_theme_font_size_override("font_size", 12)
	value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	row.add_child(value)

	slider.value_changed.connect(func(v: float) -> void:
		if to_bridge:
			ImuSettings.set_tuning(key, v)
			_queue_push()
		else:
			ImuSettings.set_assist(key, v)
		value.text = _format_value(key, v))
	value.text = _format_value(key, slider.value)
	_sliders[key] = slider


func _format_value(key: String, value: float) -> String:
	if key.ends_with("_dps") or key.ends_with("_ms") or key.ends_with("_deg"):
		return "%.0f" % value
	return "%.2f" % value


# ----------------------------------------------------------------------
# Reacting
# ----------------------------------------------------------------------
func _on_toggled(pressed: bool) -> void:
	_panel.visible = pressed
	ImuSettings.panel_open = pressed
	ImuSettings.save_settings()
	if pressed:
		ImuSettings.request_config()


func _queue_push() -> void:
	_push_in = PUSH_DELAY


func _refresh_controls() -> void:
	## Put the stored values back into the widgets, without firing their
	## handlers back at the settings they came from.
	var index := FRONTS.find(ImuSettings.front)
	if index >= 0 and _front_picker.selected != index:
		_front_picker.select(index)
	for key in _sliders:
		var slider: HSlider = _sliders[key]
		var value: float = float(ImuSettings.get(key))
		if not is_equal_approx(slider.value, value):
			slider.set_value_no_signal(value)
			# The label is not driven by the signal that was just skipped.
			var row := slider.get_parent()
			var label: Label = row.get_child(row.get_child_count() - 1)
			label.text = _format_value(key, value)
	if _rows.has("calibrated_box"):
		_rows["calibrated_box"].set_pressed_no_signal(ImuSettings.calibrated)
	if _rows.has("arrow_box"):
		_rows["arrow_box"].set_pressed_no_signal(ImuSettings.show_arrow)
	if _rows.has("hits_box"):
		_rows["hits_box"].set_pressed_no_signal(ImuSettings.colour_only_hits)
	if _rows.has("only_arrows_box"):
		_rows["only_arrows_box"].set_pressed_no_signal(ImuSettings.arrow_flicks_only)


func _refresh_readouts() -> void:
	_set_row("bridge", ImuInput.status_text if ImuInput.link_up
		else "no bridge on :%d" % ImuInput.port,
		Color(0.6, 1.0, 0.7) if ImuInput.link_up else Color(1.0, 0.7, 0.7))
	# Three states, because "the port is open" and "the board is sending" are
	# not the same thing and the difference is invisible everywhere else: a
	# port that opens and then delivers nothing reports a happy link and no
	# flicks, for ever.
	if ImuInput.transport == "demo":
		_set_row("board", "demo mode -- made-up flicks, no board",
			Color(0.8, 0.8, 1.0))
	elif not ImuInput.board_connected:
		_set_row("board", "no board -- " + ImuInput.status_text,
			Color(1.0, 0.8, 0.45))
	elif ImuInput.board_stalled:
		_set_row("board", "FROZEN: streaming, but the readings never change. "
			+ "Unplug the cable and plug it back in -- a reset leaves the "
			+ "sensor powered and holding its state.", Color(1.0, 0.55, 0.55))
	elif ImuInput.board_rate_hz < 1.0:
		_set_row("board", "port open, but no samples -- the board is not "
			+ "streaming (try replugging it)", Color(1.0, 0.8, 0.45))
	else:
		_set_row("board", "%.0f Hz" % ImuInput.board_rate_hz,
			Color(0.75, 0.85, 1.0))

	var lost := ImuInput.dropped_count()
	_set_row("counts", "%d flicks   %d refused%s" % [
		ImuInput.flicks_received, ImuInput.refused_count,
		"   %d lost" % lost if lost > 0 else ""], Color(1, 1, 1, 0.85))
	_set_row("refusal", ImuInput.last_refusal if ImuInput.last_refusal != ""
		else "none", Color(1.0, 0.85, 0.85, 0.9))

	var threshold: float = maxf(1.0, ImuInput.flick_threshold_dps)
	var bar: ProgressBar = _rows["swing_bar"]
	bar.max_value = threshold
	bar.value = minf(ImuInput.live_swing_dps, threshold)
	if is_nan(ImuInput.live_angle_deg):
		_set_row("bearing", "-- (no motion records)", Color(1, 1, 1, 0.5))
	else:
		_set_row("bearing", "%.0f deg  ->  lane %d" % [
			ImuInput.live_angle_deg, _lane_of(ImuInput.live_angle_deg)],
			Color(1, 1, 1, 0.85))
	_set_row("swing", "%.0f of %.0f dps%s" % [
		ImuInput.live_swing_dps, threshold,
		"   (would count)" if ImuInput.live_swing_dps >= threshold else ""],
		Color(0.7, 1.0, 0.8) if ImuInput.live_swing_dps >= threshold
			else Color(1, 1, 1, 0.7))

	if is_nan(ImuInput.last_bearing_deg):
		_set_row("flick", "none yet", Color(1, 1, 1, 0.5))
	else:
		var angle := ImuInput.game_angle_of(ImuInput.last_bearing_deg)
		_set_row("flick", "lane %d   strength %.2f   lag %.0f ms" % [
			_lane_of(angle), ImuInput.last_strength, ImuInput.last_lag_ms],
			Color(1, 1, 1, 0.85))

	_set_row("hit_rate", "%d hit   %d hit nothing" % [_lane_hits, _lane_misses],
		Color(1, 1, 1, 0.7))

	if not ImuSettings.aim_corrected():
		_set_row("aim", "none -- bearings used as reported", Color(1, 1, 1, 0.5))
	else:
		var turn: float = rad_to_deg(angle_difference(
			0.0, deg_to_rad(ImuSettings.bearing_offset_deg)))
		_set_row("aim", "%s%+.0f deg" % [
			"mirrored, " if ImuSettings.bearing_flip else "", turn],
			Color(0.75, 0.95, 1.0))
	if ImuInput.board_gyro_bias.size() == 3:
		_set_row("stored_bias", "%+.3f  %+.3f  %+.3f dps" % [
			float(ImuInput.board_gyro_bias[0]), float(ImuInput.board_gyro_bias[1]),
			float(ImuInput.board_gyro_bias[2])], Color(1, 1, 1, 0.7))
	else:
		_set_row("stored_bias", "not read yet", Color(1, 1, 1, 0.4))


func _set_row(key: String, text: String, colour: Color) -> void:
	var label: Label = _rows.get(key)
	if label == null:
		return
	label.text = text
	label.modulate = colour


## The game's own lane layout, so the panel names the lane the game would.
func _lane_of(angle_deg: float) -> int:
	var centres := {1: 60.0, 2: 0.0, 3: 300.0, 4: 240.0, 5: 180.0, 6: 120.0}
	var a := fposmod(angle_deg, 360.0)
	var best := 1
	var best_distance := 1e9
	for lane in centres:
		var d: float = absf(float(centres[lane]) - a)
		d = minf(d, 360.0 - d)
		if d < best_distance:
			best_distance = d
			best = int(lane)
	return best


func _on_front_suggested(record: Dictionary) -> void:
	_suggested_front = String(record.get("front", ""))
	var error := float(record.get("error_deg", 0.0))
	var current := String(record.get("current", ""))
	if _suggested_front == current:
		_learn_result.text = ("that flick fits the current axis %s to within "
			+ "%.0f deg -- nothing to change") % [current, error]
		_learn_apply.visible = false
		return
	_learn_result.text = ("that flick looks like front %s (off by %.0f deg); "
		+ "you are running %s") % [_suggested_front, error, current]
	_learn_apply.text = "apply front " + _suggested_front
	_learn_apply.visible = true


func _on_rest_measured(record: Dictionary) -> void:
	var verdict := String(record.get("verdict", ""))
	if verdict == "moved":
		_rest_result.text = ("the board moved during the measurement (peaked "
			+ "%.0f dps) -- put it down and try again") % float(record.get("peak_dps", 0.0))
		return
	var bias: Array = record.get("bias", [0, 0, 0])
	_rest_result.text = "%s: %.2f dps left over  (%+.2f %+.2f %+.2f)" % [
		{"good": "good", "fair": "fair", "poor": "poor"}.get(verdict, verdict),
		float(record.get("bias_dps", 0.0)),
		float(bias[0]), float(bias[1]), float(bias[2])]
	if verdict != "good":
		_rest_result.text += "  --   'write to board' folds this into its calibration"


func _on_bias_written(record: Dictionary) -> void:
	_bias_result.text = String(record.get("detail", ""))
	if not bool(record.get("ok", false)):
		_bias_result.modulate = Color(1.0, 0.75, 0.75)
	else:
		_bias_result.modulate = Color(0.7, 1.0, 0.8)


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
func _on_export_pressed() -> void:
	_open_dialog(FileDialog.FILE_MODE_SAVE_FILE, "imu_settings.json",
		func(path: String) -> void:
			var problem := ImuSettings.export_to(path)
			_file_note.text = problem if problem != "" else "exported to " + path)


func _on_import_pressed() -> void:
	_open_dialog(FileDialog.FILE_MODE_OPEN_FILE, "",
		func(path: String) -> void:
			var problem := ImuSettings.import_from(path)
			if problem != "":
				_file_note.text = problem
				return
			_file_note.text = "imported " + path.get_file() + " and sent it to the bridge"
			_refresh_controls())


func _open_dialog(mode: int, suggested: String, then: Callable) -> void:
	# One dialog reused, with its handler swapped: two dialogs would need two
	# lots of teardown, and leaving an old connection attached is how "import"
	# ends up also exporting.
	for connection in _dialog.file_selected.get_connections():
		_dialog.file_selected.disconnect(connection["callable"])
	_dialog.file_mode = mode
	_dialog.title = "Export IMU settings" if mode == FileDialog.FILE_MODE_SAVE_FILE \
		else "Import IMU settings"
	if suggested != "":
		_dialog.current_file = suggested
	_dialog.file_selected.connect(then)
	_dialog.popup_centered()
