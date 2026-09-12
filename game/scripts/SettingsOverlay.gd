extends CanvasLayer
## The settings menu: a gear button, and everything behind it, on every
## screen. Replaces the old ImuDebugPanel -- this is a menu a player is meant
## to use, not a debug dump, so it is organised by what someone came here to
## do (Audio, Controllers, Gameplay) with the detection internals collapsed
## into an Advanced tab rather than the only tab.
##
## Built in code, like ImuDebugPanel was, and for the same reason: it has to
## be available on the title screen and in the middle of a song without
## either scene knowing anything about it. A CanvasLayer autoload gets that
## for free -- it exists for the life of the process, not the scene.
##
## Nothing here is computed locally for the IMU tabs. Detection lives in the
## bridge, so a control sends its change there and displays what the bridge
## reports back; Settings.applied is that echo.

const FRONTS: PackedStringArray = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]

## Directions to aim a learning flick at, as bearings clockwise from up. Only
## the four square ones: they are the ones a person can make accurately
## without thinking about it, which is the whole requirement for a reference
## gesture.
const LEARN_DIRECTIONS := [
	["up", 0.0], ["right", 90.0], ["down", 180.0], ["left", 270.0],
]

## How long after a slider moves before the bridge is told. Long enough that a
## drag is one message rather than sixty, short enough to feel immediate.
const PUSH_DELAY := 0.25

## Pixels of list per wheel notch.
const WHEEL_STEP := 56

## Records kept in the Advanced tab's raw log.
const LOG_LINES := 24

var _gear: Button
var _panel: PanelContainer
var _tabs: TabContainer
var _open: bool = false

var _rows: Dictionary = {}          # name -> Label, for the live readouts
var _sliders: Dictionary = {}
var _dialog: FileDialog

var _front_picker: OptionButton
var _learn_picker: OptionButton
var _learn_result: Label
var _learn_apply: Button
var _suggested_front: String = ""
var _rest_result: Label
var _bias_result: Label
var _file_note: Label
var _profile_note: Label
var _log_view: RichTextLabel

var _push_in: float = -1.0
var _lane_hits: int = 0
var _lane_misses: int = 0

## Controllers tab: rebuilt whenever the set of active hands changes, since a
## second board can appear mid-session. Per-hand widgets are kept so refresh
## only updates labels rather than rebuilding on every frame.
var _controllers_col: VBoxContainer
var _known_hands: Array = []
var _controller_rows: Dictionary = {}   # hand -> {board, wire, scan_results}
## Which board's "scan for ports" button was pressed last, so the reply --
## scan() has no hand of its own, see Settings.scan() -- lands in the right
## section instead of whichever happened to ask first.
var _pending_scan_hand: String = ""


func _ready() -> void:
	layer = 100
	process_mode = Node.PROCESS_MODE_ALWAYS
	_build()
	_open = Settings.panel_open
	_panel.visible = _open

	Settings.changed.connect(_refresh_controls)
	Settings.front_suggested.connect(_on_front_suggested)
	Settings.rest_measured.connect(_on_rest_measured)
	Settings.bias_written.connect(_on_bias_written)
	Settings.scan_received.connect(_on_scan_received)
	TapInputBus.tap_judged.connect(func(source: String, hit: bool) -> void:
		if source != "imu":
			return
		if hit:
			_lane_hits += 1
		else:
			_lane_misses += 1)
	ImuInput.flick_received.connect(_on_flick_logged)
	ImuInput.flick_refused.connect(_on_refusal_logged)

	_refresh_controls()
	Settings.request_config()
	set_process(true)


func _input(event: InputEvent) -> void:
	# Esc only ever closes this -- it never opens it. Opening is the gear's
	# job alone, so an Esc pressed with the overlay closed reaches whatever
	# else binds it (Gameplay.gd's "back to the title") completely normally.
	if _open and event is InputEventKey and event.pressed and not event.echo \
			and event.keycode == KEY_ESCAPE:
		_toggle()
		get_viewport().set_input_as_handled()
		return

	# Route the wheel to whichever tab is showing, wherever over the panel it
	# lands -- see _new_tab() for why a bare ScrollContainer is not enough.
	if not _open or not (event is InputEventMouseButton) or not event.pressed:
		return
	var step := 0
	if event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
		step = 1
	elif event.button_index == MOUSE_BUTTON_WHEEL_UP:
		step = -1
	if step == 0 or not _panel.get_global_rect().has_point(event.position):
		return
	var scroll := _tabs.get_current_tab_control() as ScrollContainer
	if scroll:
		scroll.scroll_vertical += step * WHEEL_STEP
	get_viewport().set_input_as_handled()


func _process(delta: float) -> void:
	if _push_in > 0.0:
		_push_in -= delta
		if _push_in <= 0.0:
			Settings.push_to_bridge()
			Settings.save_settings()
	if not _open:
		return
	_refresh_controllers_tab()
	_refresh_readouts()


# ----------------------------------------------------------------------
# Open / close
# ----------------------------------------------------------------------
func _toggle() -> void:
	_open = not _open
	_panel.visible = _open
	Settings.panel_open = _open
	Settings.save_settings()
	_apply_pause(_open)
	if _open:
		Settings.request_config()
		_refresh_controls()


## Pauses the current scene through its own pause path rather than the
## engine's, if it has one -- see Gameplay.set_paused() for why.
func _apply_pause(pause: bool) -> void:
	var scene := get_tree().current_scene
	if scene and scene.has_method("set_paused"):
		scene.set_paused(pause)


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------
func _build() -> void:
	var root := Control.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(root)

	_gear = Button.new()
	_gear.text = "⚙"          # gear glyph -- no icon asset exists yet
	_gear.tooltip_text = "Settings"
	_gear.set_anchors_preset(Control.PRESET_TOP_RIGHT)
	_gear.offset_left = -52.0
	_gear.offset_top = 12.0
	_gear.offset_right = -12.0
	_gear.offset_bottom = 52.0
	_gear.add_theme_font_size_override("font_size", 22)
	_gear.modulate = Color(1, 1, 1, 0.7)
	_gear.pressed.connect(_toggle)
	root.add_child(_gear)

	_panel = PanelContainer.new()
	_panel.anchor_left = 1.0
	_panel.anchor_top = 0.0
	_panel.anchor_right = 1.0
	_panel.anchor_bottom = 1.0
	_panel.offset_left = -480.0
	_panel.offset_top = 60.0
	_panel.offset_right = -12.0
	_panel.offset_bottom = -12.0
	_panel.grow_horizontal = Control.GROW_DIRECTION_BEGIN
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.05, 0.04, 0.09, 0.94)
	style.border_color = Color(0.55, 0.48, 0.85, 0.7)
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.set_content_margin_all(6)
	_panel.add_theme_stylebox_override("panel", style)
	root.add_child(_panel)

	_tabs = TabContainer.new()
	_panel.add_child(_tabs)

	_build_audio_tab()
	_build_controllers_tab()
	_build_gameplay_tab()
	_build_advanced_tab()      # last: collapsed by default, not the one shown

	_dialog = FileDialog.new()
	_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_dialog.add_filter("*.json", "Settings profile")
	_dialog.size = Vector2i(760, 520)
	root.add_child(_dialog)


## One tab: a scroll container holding a column, with wheel scrolling routed
## to it from anywhere over the panel -- see _input(). Sliders and rows below
## set their own mouse filters so the wheel reaches this rather than being
## quietly swallowed by whatever the pointer happens to be over.
func _new_tab(title: String) -> VBoxContainer:
	var scroll := ScrollContainer.new()
	scroll.name = title
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_tabs.add_child(scroll)

	var column := VBoxContainer.new()
	column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	column.custom_minimum_size = Vector2(420, 0)
	column.add_theme_constant_override("separation", 4)
	column.mouse_filter = Control.MOUSE_FILTER_IGNORE
	scroll.add_child(column)
	return column


func _build_audio_tab() -> void:
	var column := _new_tab("Audio")
	_heading(column, "Volume")
	_note(column, "Applied straight to the audio buses (Master / Music / SFX).")
	_volume_slider(column, "vol_master", "master")
	_volume_slider(column, "vol_music", "music")
	_volume_slider(column, "vol_sfx", "sfx")


func _build_controllers_tab() -> void:
	_controllers_col = _new_tab("Controllers")
	_heading(_controllers_col, "Boards")
	_note(_controllers_col, "One section per board. Switching transport reopens "
		+ "that board's link without restarting the bridge.")


func _build_gameplay_tab() -> void:
	var column := _new_tab("Gameplay")
	_heading(column, "Timing")
	_slider(column, "audio_offset_ms", "note delay", -300.0, 300.0, 5.0, false)
	_note(column, "Milliseconds to shift the chart's audio against the notes. "
		+ "Same as the [ ] ; ' keys in a song.")

	_heading(column, "Leniency")
	_slider(column, "lane_tolerance_deg", "aim tolerance", 30.0, 100.0, 1.0, false)
	_slider(column, "timing_scale", "window stretch", 1.0, 4.0, 0.05, false)
	_note(column, "Aim tolerance is how far off a lane a flick may point and "
		+ "still reach the note in it. Window stretch multiplies the timing "
		+ "windows for flicks alone -- keys and clicks are judged the same as "
		+ "always.")

	_heading(column, "Profile")
	_note(column, "A tuning as a file: one per board, or to carry between "
		+ "machines. Load and Save write every tab here, not just this one.")
	var row := _new_row(column)
	var load_button := Button.new()
	load_button.text = "load..."
	load_button.pressed.connect(_on_import_pressed)
	row.add_child(load_button)
	var save_button := Button.new()
	save_button.text = "save as..."
	save_button.pressed.connect(_on_export_pressed)
	row.add_child(save_button)
	var defaults := Button.new()
	defaults.text = "defaults"
	defaults.pressed.connect(func() -> void:
		Settings.reset_to_defaults()
		_profile_note.text = "back to the defaults")
	row.add_child(defaults)
	_profile_note = Label.new()
	_profile_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_profile_note.add_theme_font_size_override("font_size", 11)
	column.add_child(_profile_note)


func _build_advanced_tab() -> void:
	var column := _new_tab("Advanced")
	_build_link(column)
	_build_live(column)
	_build_orientation(column)
	_build_sensitivity(column)
	_build_accuracy(column)
	_build_display(column)
	_build_log(column)
	_build_storage(column)


func _build_link(column: VBoxContainer) -> void:
	_heading(column, "Link")
	_readout(column, "bridge", "bridge")
	_readout(column, "board", "board")
	_readout(column, "counts", "flicks")
	_readout(column, "refusal", "last refusal")


func _build_live(column: VBoxContainer) -> void:
	_heading(column, "Live")
	_readout(column, "bearing", "pointing")
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
		+ "flicks land in the wrong lane, or read as rolls and are refused. "
		+ "The Calibrate button on the Controllers tab also sets this, as "
		+ "part of a full setup.")

	var row := _new_row(column)
	var label := Label.new()
	label.text = "front axis"
	label.custom_minimum_size = Vector2(120, 0)
	row.add_child(label)
	_front_picker = OptionButton.new()
	for choice in FRONTS:
		_front_picker.add_item(choice)
	_front_picker.item_selected.connect(func(index: int) -> void:
		Settings.set_tuning("front", FRONTS[index])
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
		Settings.learn_front(float(chosen[1])))
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
		Settings.set_tuning("front", _suggested_front)
		_learn_apply.visible = false
		_queue_push()
		_refresh_controls())
	column.add_child(_learn_apply)

	_slider(column, "sector_offset_deg", "lane offset", 0.0, 60.0, 1.0)


func _build_sensitivity(column: VBoxContainer) -> void:
	_heading(column, "Sensitivity")
	_note(column, "How hard a movement has to be, and how clean, before it "
		+ "counts as a flick.")
	_slider(column, "on_threshold_dps", "flick threshold", 40.0, 500.0, 5.0)
	_slider(column, "min_swing", "swing floor", 0.05, 0.95, 0.05)
	_slider(column, "min_margin", "lane margin", 0.0, 0.4, 0.01)
	_slider(column, "refractory_ms", "refractory", 60.0, 500.0, 10.0)
	_note(column, "These start almost all the way down: reaching the threshold "
		+ "is very nearly the whole test. Raise it if stray movements "
		+ "register, and the refractory if the return stroke fires a second "
		+ "flick the opposite way.")

	_heading(column, "Latency")
	_slider(column, "commit_fraction", "report at", 0.2, 0.9, 0.05)
	_note(column, "The bridge stops measuring once the rotation has fallen to "
		+ "this fraction of its own peak. Higher reports sooner off less of "
		+ "the movement; it does not change scoring, only how quickly the "
		+ "screen answers.")


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
		Settings.measure_rest(2.0))
	row.add_child(measure)
	var write := Button.new()
	write.text = "write to board"
	write.pressed.connect(func() -> void:
		_bias_result.text = "writing..."
		Settings.write_bias())
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
	calibrated.button_pressed = Settings.calibrated
	calibrated.toggled.connect(func(on: bool) -> void:
		Settings.set_tuning("calibrated", on)
		_queue_push())
	column.add_child(calibrated)
	_rows["calibrated_box"] = calibrated


func _build_display(column: VBoxContainer) -> void:
	_heading(column, "Display")
	var arrow := CheckBox.new()
	arrow.text = "show the arrow  (I)"
	arrow.button_pressed = Settings.show_arrow
	arrow.toggled.connect(func(on: bool) -> void:
		Settings.show_arrow = on
		Settings.save_settings()
		Settings.changed.emit())
	column.add_child(arrow)
	_rows["arrow_box"] = arrow

	var only_hits := CheckBox.new()
	only_hits.text = "colour only registered hits"
	only_hits.button_pressed = Settings.colour_only_hits
	only_hits.toggled.connect(func(on: bool) -> void:
		Settings.colour_only_hits = on
		Settings.save_settings()
		Settings.changed.emit())
	column.add_child(only_hits)
	_rows["hits_box"] = only_hits
	_note(column, "With this on, colour means one thing only: that flick hit "
		+ "a note. Everything else stays grey.")

	var only_arrows := CheckBox.new()
	only_arrows.text = "draw detected flicks only  (O)"
	only_arrows.button_pressed = Settings.arrow_flicks_only
	only_arrows.toggled.connect(func(on: bool) -> void:
		Settings.arrow_flicks_only = on
		Settings.save_settings()
		Settings.changed.emit())
	column.add_child(only_arrows)
	_rows["only_arrows_box"] = only_arrows
	_note(column, "A different question from the rule above. That one is "
		+ "about scoring; this is about detection -- with it on, the ring "
		+ "stays empty until a swing is strong and clean enough to be sent "
		+ "as a flick.")
	_readout(column, "hit_rate", "flicks on notes")


func _build_log(column: VBoxContainer) -> void:
	_heading(column, "Raw record log")
	_note(column, "Every flick and refusal, most recent first -- the same "
		+ "thing the bridge prints to its own console with -v.")
	_log_view = RichTextLabel.new()
	_log_view.custom_minimum_size = Vector2(0, 160)
	_log_view.scroll_active = true
	_log_view.bbcode_enabled = true
	_log_view.add_theme_font_size_override("normal_font_size", 11)
	column.add_child(_log_view)


func _build_storage(column: VBoxContainer) -> void:
	_heading(column, "Settings file")
	var row := _new_row(column)

	var save := Button.new()
	save.text = "save"
	save.pressed.connect(func() -> void:
		Settings.save_settings()
		_file_note.text = "saved to " + Settings.save_location())
	row.add_child(save)

	var export_button := Button.new()
	export_button.text = "export..."
	export_button.pressed.connect(_on_export_pressed)
	row.add_child(export_button)

	var import_button := Button.new()
	import_button.text = "import..."
	import_button.pressed.connect(_on_import_pressed)
	row.add_child(import_button)

	_file_note = Label.new()
	_file_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_file_note.add_theme_font_size_override("font_size", 11)
	_file_note.text = "saved automatically to " + Settings.save_location()
	column.add_child(_file_note)


# ----------------------------------------------------------------------
# Small builders
# ----------------------------------------------------------------------
func _new_row(parent: Node) -> HBoxContainer:
	## A row that does not swallow the mouse wheel -- see _new_tab().
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
	# under the pointer -- see _new_tab().
	slider.scrollable = false
	slider.mouse_filter = Control.MOUSE_FILTER_PASS
	slider.value = Settings.get(key)
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
			Settings.set_tuning(key, v)
			_queue_push()
		else:
			Settings.set_assist(key, v)
		value.text = _format_value(key, v))
	value.text = _format_value(key, slider.value)
	_sliders[key] = slider


## A 0..1 slider over an audio() key, shown as a percentage.
func _volume_slider(column: VBoxContainer, key: String, caption: String) -> void:
	var row := _new_row(column)
	var name_label := Label.new()
	name_label.text = caption
	name_label.custom_minimum_size = Vector2(110, 0)
	name_label.add_theme_font_size_override("font_size", 12)
	row.add_child(name_label)

	var slider := HSlider.new()
	slider.min_value = 0.0
	slider.max_value = 1.0
	slider.step = 0.01
	slider.scrollable = false
	slider.mouse_filter = Control.MOUSE_FILTER_PASS
	slider.value = Settings.get(key)
	slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	slider.custom_minimum_size = Vector2(150, 0)
	row.add_child(slider)

	var value := Label.new()
	value.custom_minimum_size = Vector2(48, 0)
	value.add_theme_font_size_override("font_size", 12)
	value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	row.add_child(value)

	slider.value_changed.connect(func(v: float) -> void:
		Settings.set_audio(key, v)
		value.text = "%d%%" % roundi(v * 100.0))
	value.text = "%d%%" % roundi(slider.value * 100.0)
	_sliders[key] = slider


func _format_value(key: String, value: float) -> String:
	if key.ends_with("_dps") or key.ends_with("_ms") or key.ends_with("_deg"):
		return "%.0f" % value
	return "%.2f" % value


# ----------------------------------------------------------------------
# Controllers tab: rebuilt when the set of boards changes
# ----------------------------------------------------------------------
func _refresh_controllers_tab() -> void:
	var hands: Array = ImuInput.active_hands()
	if hands == _known_hands:
		_update_controller_rows(hands)
		return
	_known_hands = hands.duplicate()
	for child in _controllers_col.get_children():
		child.queue_free()
	_controller_rows.clear()
	_heading(_controllers_col, "Boards")
	_note(_controllers_col, "One section per board. Switching transport "
		+ "reopens that board's link without restarting the bridge.")
	for hand in hands:
		_build_controller_section(hand)
	_update_controller_rows(hands)


func _build_controller_section(hand: String) -> void:
	var who: String = ImuInput.hand_label(hand).capitalize()
	_heading(_controllers_col, who)

	var board_label := Label.new()
	board_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	board_label.add_theme_font_size_override("font_size", 12)
	_controllers_col.add_child(board_label)

	var wire_row := _new_row(_controllers_col)
	var wire_name := Label.new()
	wire_name.text = "wire_ms"
	wire_name.custom_minimum_size = Vector2(80, 0)
	wire_name.add_theme_font_size_override("font_size", 12)
	wire_name.modulate = Color(1, 1, 1, 0.5)
	wire_row.add_child(wire_name)
	var wire_value := Label.new()
	wire_value.add_theme_font_size_override("font_size", 12)
	wire_row.add_child(wire_value)

	var transport_row := _new_row(_controllers_col)
	var host_edit := LineEdit.new()
	host_edit.placeholder_text = "IP for WiFi, e.g. 192.168.1.50"
	# Pre-filled with wherever this board last answered over WiFi, if it ever
	# has -- so reconnecting is "press WiFi", not "remember an address and
	# type it in again".
	host_edit.text = String(Settings.wifi_hosts.get(hand, ""))
	host_edit.custom_minimum_size = Vector2(180, 0)
	host_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	transport_row.add_child(host_edit)
	var usb_button := Button.new()
	usb_button.text = "USB"
	usb_button.pressed.connect(func() -> void:
		Settings.switch_transport(hand))
	transport_row.add_child(usb_button)
	var wifi_button := Button.new()
	wifi_button.text = "WiFi"
	wifi_button.pressed.connect(func() -> void:
		if host_edit.text.strip_edges() != "":
			Settings.switch_transport(hand, host_edit.text.strip_edges()))
	transport_row.add_child(wifi_button)
	var scan_button := Button.new()
	scan_button.text = "scan for ports"
	scan_button.tooltip_text = "List every serial port the bridge can see, in case USB found the wrong board or none"
	scan_button.pressed.connect(func() -> void:
		_pending_scan_hand = hand
		Settings.scan())
	transport_row.add_child(scan_button)

	var scan_results := VBoxContainer.new()
	_controllers_col.add_child(scan_results)

	var cal_row := _new_row(_controllers_col)
	var calibrate := Button.new()
	calibrate.text = "calibrate..."
	calibrate.pressed.connect(func() -> void:
		_open_calibration_wizard(hand))
	cal_row.add_child(calibrate)

	_controller_rows[hand] = {
		"board": board_label, "wire": wire_value, "scan_results": scan_results,
	}


func _update_controller_rows(hands: Array) -> void:
	for hand in hands:
		var rows: Dictionary = _controller_rows.get(hand, {})
		if rows.is_empty():
			continue
		var state: Dictionary = ImuInput.state_of(hand)
		var board_label: Label = rows["board"]
		if not ImuInput.hand_connected(hand):
			board_label.text = "GONE -- " + String(state.get("status", ""))
			board_label.modulate = Color(1.0, 0.8, 0.45)
		elif bool(state.get("stalled", false)):
			board_label.text = "FROZEN -- replug it"
			board_label.modulate = Color(1.0, 0.55, 0.55)
		elif ImuInput.hand_rate_hz(hand) < 1.0:
			board_label.text = "open, no samples"
			board_label.modulate = Color(1.0, 0.8, 0.45)
		else:
			board_label.text = "%s   %.0f Hz" % [
				String(state.get("transport", "?")), ImuInput.hand_rate_hz(hand)]
			board_label.modulate = Color(0.75, 0.85, 1.0)
		var wire_value: Label = rows["wire"]
		wire_value.text = "%.1f ms" % float(state.get("wire_ms", 0.0))


## Opens the in-game calibration wizard for one board, if it exists yet.
## Wired up in full once CalibrationWizard.tscn lands; a missing scene here
## fails quietly rather than with a broken-path error, since this button can
## be built before that scene exists.
func _open_calibration_wizard(hand: String) -> void:
	if not ResourceLoader.exists("res://scenes/CalibrationWizard.tscn"):
		push_warning("[settings] calibration wizard is not built yet")
		return
	var scene: PackedScene = load("res://scenes/CalibrationWizard.tscn")
	var wizard: Node = scene.instantiate()
	wizard.hand = hand
	get_tree().current_scene.add_child(wizard)


## The reply to a "scan for ports" press: one button per port found, in
## whichever board's section asked for it. Picking one pins that board to
## that exact port rather than trusting auto-detection -- for when USB found
## the wrong board, or none, with two or more connected.
func _on_scan_received(record: Dictionary) -> void:
	if _pending_scan_hand == "" or not _controller_rows.has(_pending_scan_hand):
		return
	var hand: String = _pending_scan_hand
	_pending_scan_hand = ""
	var results: VBoxContainer = _controller_rows[hand]["scan_results"]
	for child in results.get_children():
		child.queue_free()

	var ports: Array = record.get("ports", [])
	if ports.is_empty():
		var none_label := Label.new()
		none_label.text = "No serial ports found."
		none_label.add_theme_font_size_override("font_size", 11)
		none_label.modulate = Color(1, 1, 1, 0.5)
		results.add_child(none_label)
		return
	for port in ports:
		var device := String(port.get("device", ""))
		var looks_like_board := bool(port.get("looks_like_board", false))
		var button := Button.new()
		button.text = "%s  %s%s" % [device, String(port.get("description", "")),
			"  (looks like a board)" if looks_like_board else ""]
		if looks_like_board:
			button.modulate = Color(0.8, 1.0, 0.85)
		button.pressed.connect(func() -> void:
			Settings.switch_transport(hand, "", 3333, device))
		results.add_child(button)


# ----------------------------------------------------------------------
# Reacting
# ----------------------------------------------------------------------
func _queue_push() -> void:
	_push_in = PUSH_DELAY


func _refresh_controls() -> void:
	## Put the stored values back into the widgets, without firing their
	## handlers back at the settings they came from.
	var index := FRONTS.find(Settings.front)
	if index >= 0 and _front_picker.selected != index:
		_front_picker.select(index)
	for key in _sliders:
		var slider: HSlider = _sliders[key]
		var value: float = float(Settings.get(key))
		if not is_equal_approx(slider.value, value):
			slider.set_value_no_signal(value)
			var row := slider.get_parent()
			var label: Label = row.get_child(row.get_child_count() - 1)
			if key.begins_with("vol_"):
				label.text = "%d%%" % roundi(value * 100.0)
			else:
				label.text = _format_value(key, value)
	if _rows.has("calibrated_box"):
		_rows["calibrated_box"].set_pressed_no_signal(Settings.calibrated)
	if _rows.has("arrow_box"):
		_rows["arrow_box"].set_pressed_no_signal(Settings.show_arrow)
	if _rows.has("hits_box"):
		_rows["hits_box"].set_pressed_no_signal(Settings.colour_only_hits)
	if _rows.has("only_arrows_box"):
		_rows["only_arrows_box"].set_pressed_no_signal(Settings.arrow_flicks_only)


func _refresh_readouts() -> void:
	_set_row("bridge", ImuInput.status_text if ImuInput.link_up
		else "no bridge on :%d" % ImuInput.port,
		Color(0.6, 1.0, 0.7) if ImuInput.link_up else Color(1.0, 0.7, 0.7))
	if ImuInput.two_handed():
		_refresh_two_board_readouts()
		_set_row("hit_rate", "%d hit   %d hit nothing" % [
			_lane_hits, _lane_misses], Color(1, 1, 1, 0.7))
		return
	if ImuInput.transport == "demo":
		_set_row("board", "demo mode -- made-up flicks, no board",
			Color(0.8, 0.8, 1.0))
	elif not ImuInput.board_connected:
		_set_row("board", "no board -- " + ImuInput.status_text,
			Color(1.0, 0.8, 0.45))
	elif ImuInput.board_stalled:
		_set_row("board", "FROZEN: streaming, but the readings never change. "
			+ "Unplug the cable and plug it back in.", Color(1.0, 0.55, 0.55))
	elif ImuInput.board_rate_hz < 1.0:
		_set_row("board", "port open, but no samples -- try replugging it",
			Color(1.0, 0.8, 0.45))
	else:
		_set_row("board", "%.0f Hz  (wire %.1f ms)" % [
			ImuInput.board_rate_hz, ImuInput.board_wire_ms],
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


## The Link and Live rows, one board's worth of each, side by side -- with two
## boards the answers differ per board, and folding them into one set of
## numbers is the one display that can be wrong while looking right.
func _refresh_two_board_readouts() -> void:
	var board_parts: PackedStringArray = []
	var count_parts: PackedStringArray = []
	var refusal_parts: PackedStringArray = []
	var bearing_parts: PackedStringArray = []
	var swing_parts: PackedStringArray = []
	var flick_parts: PackedStringArray = []
	var worst := Color(0.75, 0.85, 1.0)
	var peak_swing: float = 0.0
	var peak_threshold: float = 1.0

	for hand in ImuInput.hands:
		var who: String = ImuInput.hand_label(hand)
		var state: Dictionary = ImuInput.state_of(hand)

		if not ImuInput.hand_connected(hand):
			board_parts.append("%s GONE -- %s" % [who, state["status"]])
			worst = Color(1.0, 0.8, 0.45)
		elif bool(state["stalled"]):
			board_parts.append("%s FROZEN -- replug it" % who)
			worst = Color(1.0, 0.55, 0.55)
		elif ImuInput.hand_rate_hz(hand) < 1.0:
			board_parts.append("%s open, no samples" % who)
			worst = Color(1.0, 0.8, 0.45)
		else:
			board_parts.append("%s %.0f Hz" % [who, ImuInput.hand_rate_hz(hand)])

		var lost: int = int(state["dropped"])
		count_parts.append("%s %d flicks  %d refused%s" % [
			who, int(state["flicks"]), int(state["refused"]),
			"  %d lost" % lost if lost > 0 else ""])
		refusal_parts.append("%s %s" % [who,
			state["refusal"] if String(state["refusal"]) != "" else "none"])

		var angle: float = ImuInput.hand_angle(hand)
		if is_nan(angle):
			bearing_parts.append("%s -- (no motion)" % who)
		else:
			bearing_parts.append("%s %.0f deg -> lane %d" % [
				who, angle, _lane_of(angle)])

		var threshold: float = maxf(1.0, ImuInput.hand_threshold_dps(hand))
		var swing: float = ImuInput.hand_swing_dps(hand)
		swing_parts.append("%s %.0f of %.0f%s" % [who, swing, threshold,
			" (counts)" if swing >= threshold else ""])
		if swing >= peak_swing:
			peak_swing = swing
			peak_threshold = threshold

		if is_nan(float(state["bearing"])):
			flick_parts.append("%s none yet" % who)
		else:
			var went: float = ImuInput.game_angle_of(float(state["bearing"]), hand)
			flick_parts.append("%s lane %d  str %.2f  lag %.0f ms" % [
				who, _lane_of(went), float(state["strength"]),
				float(state["lag_ms"])])

	_set_row("board", "   ".join(board_parts), worst)
	_set_row("counts", "   ".join(count_parts), Color(1, 1, 1, 0.85))
	_set_row("refusal", "   ".join(refusal_parts), Color(1.0, 0.85, 0.85, 0.9))
	_set_row("bearing", "   ".join(bearing_parts), Color(1, 1, 1, 0.85))
	_set_row("swing", "   ".join(swing_parts), Color(1, 1, 1, 0.7))
	_set_row("flick", "   ".join(flick_parts), Color(1, 1, 1, 0.85))

	var bar: ProgressBar = _rows["swing_bar"]
	bar.max_value = peak_threshold
	bar.value = minf(peak_swing, peak_threshold)


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
# Raw record log
# ----------------------------------------------------------------------
func _on_flick_logged(record: Dictionary) -> void:
	var hand := String(record.get("hand", ""))
	_append_log("[color=#a8ffb0]flick[/color] %s bearing %.0f  strength %.2f" % [
		"[%s] " % hand if hand != "" else "",
		float(record.get("bearing", 0.0)), float(record.get("strength", 0.0))])


func _on_refusal_logged(record: Dictionary) -> void:
	var hand := String(record.get("hand", ""))
	_append_log("[color=#ffb0b0]refused[/color] %s%s" % [
		"[%s] " % hand if hand != "" else "",
		String(record.get("detail", record.get("reason", "")))])


func _append_log(line: String) -> void:
	if _log_view == null:
		return
	_log_view.append_text(line + "\n")
	# Trim from the top rather than letting the buffer grow for the life of
	# the process -- this is a live diagnostic view, not a transcript.
	var lines := _log_view.get_parsed_text().split("\n")
	if lines.size() > LOG_LINES:
		_log_view.clear()
		# get_parsed_text() strips bbcode, so the trimmed re-append loses
		# colour on old lines -- acceptable, since only the newest lines
		# (colour intact, appended after this) are what anyone is reading.
		for kept in lines.slice(lines.size() - LOG_LINES, lines.size()):
			_log_view.append_text(kept + "\n")


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
func _on_export_pressed() -> void:
	_open_dialog(FileDialog.FILE_MODE_SAVE_FILE, "settings_profile.json",
		func(path: String) -> void:
			var problem := Settings.export_to(path)
			var note := problem if problem != "" else "exported to " + path
			_file_note.text = note
			_profile_note.text = note)


func _on_import_pressed() -> void:
	_open_dialog(FileDialog.FILE_MODE_OPEN_FILE, "",
		func(path: String) -> void:
			var problem := Settings.import_from(path)
			var note: String
			if problem != "":
				note = problem
			else:
				note = "imported " + path.get_file() + " and sent it to the bridge"
				_refresh_controls()
			_file_note.text = note
			_profile_note.text = note)


func _open_dialog(mode: int, suggested: String, then: Callable) -> void:
	# One dialog reused, with its handler swapped: two dialogs would need two
	# lots of teardown, and leaving an old connection attached is how "import"
	# ends up also exporting.
	for connection in _dialog.file_selected.get_connections():
		_dialog.file_selected.disconnect(connection["callable"])
	_dialog.file_mode = mode
	_dialog.title = "Export settings" if mode == FileDialog.FILE_MODE_SAVE_FILE \
		else "Import settings"
	if suggested != "":
		_dialog.current_file = suggested
	_dialog.file_selected.connect(then)
	_dialog.popup_centered()
