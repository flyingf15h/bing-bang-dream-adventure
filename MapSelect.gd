extends Control

const TapEvent = preload("res://TapInputBus.gd").TapEvent

const JACKET: Texture2D = preload("res://assets/jacket_badapple.png")
const BG_ART: Texture2D = preload("res://assets/game_loss.png")

@export var beatmap_path: String = "res://badapple_hex.json"
@export var song_title: String = "Bad Apple!!"
@export var song_bpm: float = 138.0

# Palette pulled from the intro logo.
const SEL_BLUE     := Color(0.537, 0.596, 0.878)   # second shade of blue
const SEL_PURPLE   := Color(0.647, 0.404, 0.847)   # outline / accent purple
const SEL_INK_DARK := Color(0.145, 0.125, 0.212)
const SEL_INK_SOFT := Color(0.451, 0.427, 0.529)
const SEL_BG       := Color(0.957, 0.949, 0.976)

@onready var map_button: Button = $MapButton
@onready var hint: Label = $Hint

var font_bold: FontVariation
var font_thin: FontVariation
var font_heavy: FontVariation
var _pulse: float = 0.0
var _btn_base_y: float = 0.0
var _hover_lift: float = 0.0
var _hovered: bool = false


func _ready() -> void:
	get_viewport().msaa_2d = Viewport.MSAA_4X

	font_bold = FontVariation.new()
	font_bold.base_font = ThemeDB.fallback_font
	font_bold.variation_embolden = 0.42
	font_thin = FontVariation.new()
	font_thin.base_font = ThemeDB.fallback_font
	font_thin.variation_embolden = 0.0
	font_thin.spacing_glyph = 1
	font_heavy = FontVariation.new()
	font_heavy.base_font = ThemeDB.fallback_font
	font_heavy.variation_embolden = 0.72
	font_heavy.spacing_glyph = 1

	# The scene's own dark background and heading would sit on top of this
	# node's _draw(), so stand them down and let _draw() own the screen.
	_quiet_scene_chrome()

	_read_beatmap_meta()

	map_button.pressed.connect(_on_map_confirmed)
	map_button.mouse_entered.connect(func(): _hovered = true)
	map_button.mouse_exited.connect(func(): _hovered = false)
	map_button.focus_entered.connect(func(): _hovered = true)
	map_button.focus_exited.connect(func(): _hovered = false)
	map_button.grab_focus()
	TapInputBus.tap.connect(_on_tap)

	_style_button()
	if ImuInput.link_up:
		hint.text = "flick the board, click the map, or press ENTER to start"
	else:
		hint.text = "click the map or press ENTER to start"
	_style_hint()

	set_process(true)
	get_viewport().size_changed.connect(_layout)
	_layout()


func _quiet_scene_chrome() -> void:
	## Hide any solid background panel and any label other than the hint, so
	## the light backdrop drawn below is what the player actually sees.
	for child in get_children():
		if child == map_button or child == hint:
			continue
		if child is ColorRect or child is Panel or child is TextureRect:
			child.visible = false
		elif child is Label:
			child.visible = false


func _read_beatmap_meta() -> void:
	## Prefer the chart's own title / bpm when the file is readable.
	if not FileAccess.file_exists(beatmap_path):
		return
	var f := FileAccess.open(beatmap_path, FileAccess.READ)
	if f == null:
		return
	var data = JSON.parse_string(f.get_as_text())
	f.close()
	if typeof(data) != TYPE_DICTIONARY:
		return
	if data.has("title"):
		song_title = String(data["title"])
	if data.has("bpm"):
		var raw: Variant = data["bpm"]
		match typeof(raw):
			TYPE_FLOAT, TYPE_INT:
				song_bpm = float(raw)
			TYPE_STRING, TYPE_STRING_NAME:
				var s := String(raw)
				if s.is_valid_float():
					song_bpm = s.to_float()


func _process(delta: float) -> void:
	_pulse += delta
	# Ease toward the lifted or resting height so hovering feels springy
	# rather than snapping between two positions.
	var target: float = 6.0 if _hovered else 0.0
	_hover_lift += (target - _hover_lift) * minf(1.0, delta * 22.0)
	map_button.position.y = _btn_base_y - _hover_lift
	queue_redraw()


func _style_button() -> void:
	## The button is the jacket square: art inside, thin purple outline.
	map_button.text = ""
	map_button.icon = JACKET
	map_button.expand_icon = true
	map_button.focus_mode = Control.FOCUS_ALL
	map_button.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND

	var flat := StyleBoxFlat.new()
	flat.bg_color = Color(1, 1, 1, 0.0)
	flat.border_color = Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 0.75)
	flat.set_border_width_all(1)
	flat.set_content_margin_all(0)

	var hover := flat.duplicate() as StyleBoxFlat
	hover.border_color = Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 1.0)
	hover.set_border_width_all(2)

	map_button.add_theme_stylebox_override("normal", flat)
	map_button.add_theme_stylebox_override("hover", hover)
	map_button.add_theme_stylebox_override("pressed", hover)
	map_button.add_theme_stylebox_override("focus", hover)


func _style_hint() -> void:
	hint.add_theme_font_override("font", font_thin)
	hint.add_theme_font_size_override("font_size", 15)
	hint.add_theme_color_override("font_color",
		Color(SEL_INK_SOFT.r, SEL_INK_SOFT.g, SEL_INK_SOFT.b, 0.95))
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER


func _layout() -> void:
	var vp: Vector2 = get_viewport_rect().size
	var sq: float = minf(vp.x * 0.30, vp.y * 0.44)
	map_button.size = Vector2(sq, sq)
	# Sits under the heading, with room for the song title just above it.
	_btn_base_y = vp.y * 0.29
	map_button.position = Vector2(vp.x * 0.5 - sq * 0.5, _btn_base_y - _hover_lift)

	hint.size = Vector2(vp.x, 22.0)
	hint.position = Vector2(0.0, vp.y - 52.0)


func _draw() -> void:
	var vp: Vector2 = get_viewport_rect().size
	var cx: float = vp.x * 0.5

	# ── Backdrop, same as the intro / results screens ────────────────────
	draw_rect(Rect2(Vector2.ZERO, vp), SEL_BG)
	var bg_scale: float = maxf(vp.x / BG_ART.get_width(), vp.y / BG_ART.get_height())
	var bg_size: Vector2 = BG_ART.get_size() * bg_scale
	draw_texture_rect(BG_ART, Rect2((vp - bg_size) * 0.5, bg_size), false,
		Color(1, 1, 1, 0.16))
	for i in 12:
		var f: float = float(i) / 12.0
		draw_circle(Vector2(cx, vp.y * 0.52), vp.x * (0.16 + 0.48 * f),
			Color(0.62, 0.55, 0.82, 0.014 * (1.0 - f)))

	# ── Heading ──────────────────────────────────────────────────────────
	var h_txt := "SELECT MAP"
	var h_sz: int = 50
	draw_string(font_heavy, Vector2(cx - _tw(font_heavy, h_txt, h_sz) * 0.5, vp.y * 0.16),
		h_txt, 0, -1, h_sz, Color(SEL_BLUE.r, SEL_BLUE.g, SEL_BLUE.b, 0.98))

	# ── Soft pulsing halo, following the card as it lifts ────────────────
	var g: float = 0.5 + 0.5 * sin(_pulse * 2.2)
	for i in 3:
		var pad: float = 5.0 + float(i) * 4.0
		draw_rect(Rect2(map_button.position - Vector2(pad, pad),
			map_button.size + Vector2(pad * 2.0, pad * 2.0)),
			Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b,
				(0.13 - float(i) * 0.035) * (0.55 + 0.45 * g)), false, 1.2)

	# ── Song title, above the jacket (fixed, does not lift) ──────────────
	var t_sz: int = 26
	draw_string(font_bold, Vector2(cx - _tw(font_bold, song_title, t_sz) * 0.5,
		_btn_base_y - 20.0), song_title, 0, -1, t_sz,
		Color(SEL_INK_DARK.r, SEL_INK_DARK.g, SEL_INK_DARK.b, 0.97))

	# ── BPM pill, just below the jacket ──────────────────────────────────
	var sq_bottom: float = _btn_base_y + map_button.size.y
	var bpm_txt := "BPM  %d" % _bpm_int()
	var b_sz: int = 16
	var bpm_w: float = _tw(font_thin, bpm_txt, b_sz)
	var bpm_y: float = sq_bottom + 40.0
	# Taller box with a deeper top margin so the text is not crowded.
	draw_rect(Rect2(Vector2(cx - bpm_w * 0.5 - 16.0, bpm_y - 22.0),
		Vector2(bpm_w + 32.0, 32.0)),
		Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 0.55), false, 1.0)
	draw_string(font_thin, Vector2(cx - bpm_w * 0.5, bpm_y), bpm_txt, 0, -1, b_sz,
		Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 0.95))


func _bpm_int() -> int:
	## song_bpm can arrive from JSON, or from a stale value stored in the
	## scene by a previous version of this script, so it is not guaranteed to
	## be a number.  round() rejects anything that is not numeric, so coerce
	## here rather than trusting the declared type.
	var v: Variant = song_bpm
	match typeof(v):
		TYPE_FLOAT, TYPE_INT:
			return int(roundf(float(v)))
		TYPE_STRING, TYPE_STRING_NAME:
			var s := String(v)
			return int(roundf(s.to_float())) if s.is_valid_float() else 0
		_:
			return 0


func _tw(f: Font, t: String, sz: int) -> float:
	return f.get_string_size(t, 0, -1, sz).x


func _on_tap(event: TapEvent) -> void:
	if event.source != "imu":
		return
	_on_map_confirmed()


func _on_map_confirmed() -> void:
	get_tree().change_scene_to_file("res://node_2d.tscn")
