extends Control

const PHASES: Array[Texture2D] = [
	preload("res://assets/images/phase_1.png"),
	preload("res://assets/images/phase_2.png"),
	preload("res://assets/images/phase_3.png"),
	preload("res://assets/images/phase_4.png"),
	preload("res://assets/images/phase_5.png"),
]

const PHASE_HOLD: float = 0.22
const POP_DURATION: float = 0.16
const FINAL_HOLD: float = 0.5
const LOGO_FADE_DURATION: float = 0.35

const TapEvent = preload("res://autoload/TapInputBus.gd").TapEvent

@onready var logo: TextureRect = $Logo
@onready var start_button: TextureButton = $StartButton

var _hover_tween: Tween
var _button_ready := false
var _imu_label: Label


func _ready() -> void:
	logo.texture = null
	logo.modulate.a = 0.0

	start_button.modulate.a = 0.0
	start_button.disabled = true
	start_button.pivot_offset = start_button.size / 2.0
	start_button.mouse_entered.connect(_on_hover_start)
	start_button.mouse_exited.connect(_on_hover_end)
	start_button.pressed.connect(_on_start_pressed)
	_generate_click_mask()

	# The board is a valid way to press this button, so it has to be a valid
	# way to get past this screen too -- otherwise "play with the IMU" still
	# means reaching for the mouse first.
	TapInputBus.tap.connect(_on_tap)
	_build_imu_label()
	# Also here, not only in game: setting the front axis and the thresholds is
	# something you do before playing, and doing it here costs no notes.
	add_child(preload("res://scripts/ImuDebugPanel.gd").new())

	_play_intro()


func _build_imu_label() -> void:
	## A player holding only the board has no way to tell whether the bridge is
	## running, and an unresponsive title screen is indistinguishable from a
	## broken one. This says which it is.
	_imu_label = Label.new()
	_imu_label.add_theme_font_size_override("font_size", 16)
	# The title art is a bright, busy illustration and this sits on top of it,
	# so the text needs to carry its own contrast. Without the outline the
	# line is there but unreadable, which is worse than absent: the screen
	# looks like it has no IMU status at all rather than one you can't see.
	_imu_label.add_theme_constant_override("outline_size", 5)
	_imu_label.add_theme_color_override("font_outline_color", Color(0.04, 0.03, 0.10, 0.85))
	_imu_label.set_anchors_preset(Control.PRESET_BOTTOM_WIDE)
	_imu_label.offset_top = -40.0
	_imu_label.offset_bottom = -14.0
	_imu_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	add_child(_imu_label)
	_refresh_imu_label()
	ImuInput.link_changed.connect(func(_up: bool) -> void: _refresh_imu_label())
	ImuInput.board_changed.connect(func(_on: bool) -> void: _refresh_imu_label())


func _refresh_imu_label() -> void:
	if not is_instance_valid(_imu_label):
		return
	# Three states, not two. A bridge with no board behind it is the one that
	# used to read as "IMU ready", which is the worst thing it could have said:
	# it sends the player off to flick at a game that cannot possibly answer,
	# and the fault is a cable rather than anything they are doing.
	if not ImuInput.enabled:
		_imu_label.text = ""
	elif not ImuInput.link_up:
		_imu_label.text = "no IMU bridge — run  python dashboard/game_bridge.py"
		_imu_label.modulate = Color(1.0, 0.92, 0.92, 0.85)
	elif not ImuInput.board_connected:
		_imu_label.text = "bridge running, no board — " + ImuInput.status_text
		_imu_label.modulate = Color(1.0, 0.80, 0.45, 1.0)
	elif ImuInput.board_stalled:
		# Streaming perfectly and measuring nothing. Said in the imperative
		# because the fix is specific and unguessable: a reset does not clear
		# it, only unplugging does.
		_imu_label.text = "board frozen — unplug it and plug it back in (a reset will not do it)"
		_imu_label.modulate = Color(1.0, 0.55, 0.55, 1.0)
	else:
		_imu_label.text = "IMU ready — flick the board to start"
		_imu_label.modulate = Color(0.55, 1.0, 0.70, 1.0)


func _on_tap(event: TapEvent) -> void:
	# Only the IMU: a mouse click on this screen is already the button's job,
	# and routing clicks through here as well would fire it twice.
	if event.source != "imu" or not _button_ready:
		return
	_on_start_pressed()


func _generate_click_mask() -> void:
	# The button texture is a full-canvas transparent PNG (the art is drawn
	# at its real position within a big canvas), so without a mask the whole
	# screen would count as "the button." This restricts hover/click to the
	# actual opaque pixels.
	var img: Image = start_button.texture_normal.get_image()
	if img == null:
		return
	var mask := BitMap.new()
	mask.create_from_image_alpha(img)
	start_button.texture_click_mask = mask


func _play_intro() -> void:
	for phase in PHASES:
		await _pop_in_phase(phase)
		await get_tree().create_timer(PHASE_HOLD).timeout
	await get_tree().create_timer(FINAL_HOLD).timeout
	_dismiss_logo()
	_enable_button()


func _dismiss_logo() -> void:
	# The animated wordmark was just an intro flourish; once it's done, the
	# idle title screen goes back to plain art + button, so it doesn't sit
	# there covering the illustration forever.
	var tw := create_tween()
	tw.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN)
	tw.tween_property(logo, "modulate:a", 0.0, LOGO_FADE_DURATION)


func _pop_in_phase(tex: Texture2D) -> void:
	logo.texture = tex
	logo.pivot_offset = logo.size / 2.0
	logo.scale = Vector2(1.3, 1.3)
	logo.modulate.a = 0.0
	var tw := create_tween().set_parallel(true)
	tw.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	tw.tween_property(logo, "scale", Vector2.ONE, POP_DURATION)
	tw.tween_property(logo, "modulate:a", 1.0, POP_DURATION * 0.7)
	await tw.finished


func _enable_button() -> void:
	_button_ready = true
	_refresh_imu_label()
	start_button.disabled = false
	start_button.scale = Vector2(1.3, 1.3)
	var tw := create_tween().set_parallel(true)
	tw.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	tw.tween_property(start_button, "modulate:a", 1.0, 0.25)
	tw.tween_property(start_button, "scale", Vector2.ONE, 0.3)
	start_button.grab_focus()


func _on_hover_start() -> void:
	if not _button_ready:
		return
	_animate_scale(1.08)


func _on_hover_end() -> void:
	if not _button_ready:
		return
	_animate_scale(1.0)


func _animate_scale(target: float) -> void:
	if _hover_tween:
		_hover_tween.kill()
	_hover_tween = create_tween().set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	_hover_tween.tween_property(start_button, "scale", Vector2(target, target), 0.18)


func _on_start_pressed() -> void:
	get_tree().change_scene_to_file("res://scenes/MapSelect.tscn")
