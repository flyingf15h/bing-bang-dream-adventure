extends Control

const PHASES: Array[Texture2D] = [
	preload("res://assets/phase_1.png"),
	preload("res://assets/phase_2.png"),
	preload("res://assets/phase_3.png"),
	preload("res://assets/phase_4.png"),
	preload("res://assets/phase_5.png"),
]

const PHASE_HOLD: float = 0.22
const POP_DURATION: float = 0.16
const FINAL_HOLD: float = 0.5
const LOGO_FADE_DURATION: float = 0.35

const TapEvent = preload("res://TapInputBus.gd").TapEvent

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

	_play_intro()


func _build_imu_label() -> void:
	## A player holding only the board has no way to tell whether the bridge is
	## running, and an unresponsive title screen is indistinguishable from a
	## broken one. This says which it is.
	_imu_label = Label.new()
	_imu_label.add_theme_font_size_override("font_size", 15)
	_imu_label.set_anchors_preset(Control.PRESET_BOTTOM_WIDE)
	_imu_label.offset_top = -40.0
	_imu_label.offset_bottom = -14.0
	_imu_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	add_child(_imu_label)
	_refresh_imu_label()
	ImuInput.link_changed.connect(func(_up: bool) -> void: _refresh_imu_label())


func _refresh_imu_label() -> void:
	if not is_instance_valid(_imu_label):
		return
	if ImuInput.link_up:
		_imu_label.text = "IMU ready — flick the board to start"
		_imu_label.modulate = Color(0.65, 1.0, 0.75, 0.9)
	elif not ImuInput.enabled:
		_imu_label.text = ""
	else:
		_imu_label.text = "no IMU bridge — run  python dashboard/game_bridge.py"
		_imu_label.modulate = Color(1.0, 1.0, 1.0, 0.35)


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
	get_tree().change_scene_to_file("res://MapSelect.tscn")
