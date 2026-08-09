extends Control

const TapEvent = preload("res://TapInputBus.gd").TapEvent

const BG_ART: Texture2D = preload("res://assets/game_loss.png")

## The charts on offer, in the order they are listed.
##
## A map is four paths, a jacket and a caption rather than a scene of its own:
## the gameplay scene takes all of it as exported properties, so adding a chart
## is adding an entry here and dropping a JSON file next to it. An empty audio
## or video path means the chart genuinely has neither, which is a different
## thing from a file that failed to load and is treated as such.
const MAPS := [
	{
		"title": "Bad Apple!!",
		"caption": "138 BPM   141 notes",
		"beatmap": "res://badapple_hex.json",
		"audio": "res://Bad-Apple-Cut-Audio.ogg",
		"video": "res://Bad-Apple-Cut-Video.ogv",
		"jacket": "res://assets/jacket_badapple.png",
	},
	{
		"title": "Flick Test",
		"caption": "100 BPM   32 notes   no music, no video",
		"beatmap": "res://test_flicks.json",
		"audio": "",
		"video": "",
		"jacket": "res://assets/song_jacket.png",
	},
]

# Palette pulled from the intro logo.
const SEL_BLUE     := Color(0.537, 0.596, 0.878)   # second shade of blue
const SEL_PURPLE   := Color(0.647, 0.404, 0.847)   # outline / accent purple
const SEL_INK_DARK := Color(0.145, 0.125, 0.212)
const SEL_INK_SOFT := Color(0.451, 0.427, 0.529)
const SEL_BG       := Color(0.957, 0.949, 0.976)

const CARD_HEIGHT: float = 104.0

@onready var maps_box: VBoxContainer = $Maps
@onready var hint: Label = $Hint

var font_bold: FontVariation
var font_thin: FontVariation
var font_heavy: FontVariation

var _buttons: Array[Button] = []
var _selected: int = 0
var _pulse: float = 0.0


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

	for index in MAPS.size():
		var map: Dictionary = MAPS[index]
		var button := Button.new()
		button.text = "  %s\n  %s" % [map["title"], _caption_for(map)]
		button.custom_minimum_size = Vector2(420, CARD_HEIGHT)
		button.pressed.connect(_start.bind(index))
		# Moving the highlight with the board has to move the keyboard's focus
		# too, or the two disagree about what ENTER would start.
		button.focus_entered.connect(func() -> void: _selected = index)
		button.mouse_entered.connect(button.grab_focus)
		_style_card(button, map)
		maps_box.add_child(button)
		_buttons.append(button)
	_buttons[0].grab_focus()

	TapInputBus.tap.connect(_on_tap)

	if ImuInput.link_up:
		hint.text = "flick up or down to choose, sideways to start"
	else:
		hint.text = "arrow keys choose, click or press ENTER to start"
	_style_hint()

	set_process(true)


func _quiet_scene_chrome() -> void:
	## Hide any solid background panel and any label other than the hint, so
	## the light backdrop drawn below is what the player actually sees.
	for child in get_children():
		if child == maps_box or child == hint:
			continue
		if child is ColorRect or child is Panel or child is TextureRect:
			child.visible = false
		elif child is Label:
			child.visible = false


func _caption_for(map: Dictionary) -> String:
	## Prefer the chart's own title / bpm when the file is readable, so a
	## caption cannot quietly drift away from the JSON it describes.
	var caption := String(map["caption"])
	var path := String(map["beatmap"])
	if not FileAccess.file_exists(path):
		return caption
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		return caption
	var data = JSON.parse_string(f.get_as_text())
	f.close()
	if typeof(data) != TYPE_DICTIONARY or not data.has("bpm"):
		return caption
	var bpm := _as_number(data["bpm"])
	if bpm <= 0.0:
		return caption
	# Only the BPM is authoritative in the chart; the rest of the caption is
	# written here, so splice rather than replace.
	var rest: String = caption.substr(caption.find("BPM") + 3) if caption.find("BPM") != -1 else ""
	return "%d BPM%s" % [int(roundf(bpm)), rest]


func _as_number(v: Variant) -> float:
	## Values arriving from JSON are not guaranteed to be numbers.
	match typeof(v):
		TYPE_FLOAT, TYPE_INT:
			return float(v)
		TYPE_STRING, TYPE_STRING_NAME:
			var s := String(v)
			return s.to_float() if s.is_valid_float() else 0.0
		_:
			return 0.0


func _style_card(button: Button, map: Dictionary) -> void:
	## Each card is its jacket plus the chart's name, in a thin purple outline.
	var jacket_path := String(map["jacket"])
	if ResourceLoader.exists(jacket_path):
		button.icon = load(jacket_path)
		button.add_theme_constant_override("icon_max_width", int(CARD_HEIGHT) - 20)
	button.focus_mode = Control.FOCUS_ALL
	button.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	button.alignment = HORIZONTAL_ALIGNMENT_LEFT
	button.add_theme_font_override("font", font_bold)
	button.add_theme_font_size_override("font_size", 21)
	for state in ["font_color", "font_hover_color", "font_focus_color",
			"font_pressed_color"]:
		button.add_theme_color_override(state,
			Color(SEL_INK_DARK.r, SEL_INK_DARK.g, SEL_INK_DARK.b, 0.97))

	var flat := StyleBoxFlat.new()
	flat.bg_color = Color(1, 1, 1, 0.35)
	flat.border_color = Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 0.45)
	flat.set_border_width_all(1)
	flat.set_content_margin_all(10)

	var hover := flat.duplicate() as StyleBoxFlat
	hover.bg_color = Color(1, 1, 1, 0.72)
	hover.border_color = Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b, 1.0)
	hover.set_border_width_all(2)

	button.add_theme_stylebox_override("normal", flat)
	button.add_theme_stylebox_override("hover", hover)
	button.add_theme_stylebox_override("pressed", hover)
	button.add_theme_stylebox_override("focus", hover)


func _style_hint() -> void:
	hint.add_theme_font_override("font", font_thin)
	hint.add_theme_font_size_override("font_size", 15)
	hint.add_theme_color_override("font_color",
		Color(SEL_INK_SOFT.r, SEL_INK_SOFT.g, SEL_INK_SOFT.b, 0.95))
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER


func _process(delta: float) -> void:
	_pulse += delta
	queue_redraw()


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

	# ── Soft pulsing halo around whichever card is highlighted ───────────
	if _selected < _buttons.size():
		var card: Rect2 = _buttons[_selected].get_global_rect()
		var g: float = 0.5 + 0.5 * sin(_pulse * 2.2)
		for i in 3:
			var pad: float = 5.0 + float(i) * 4.0
			draw_rect(card.grow(pad),
				Color(SEL_PURPLE.r, SEL_PURPLE.g, SEL_PURPLE.b,
					(0.13 - float(i) * 0.035) * (0.55 + 0.45 * g)), false, 1.2)


func _tw(f: Font, t: String, sz: int) -> float:
	return f.get_string_size(t, 0, -1, sz).x


func _on_tap(event: TapEvent) -> void:
	## Up and down move, sideways starts.
	##
	## With one map on the list any flick could confirm it, and that is what
	## this used to do. With more than one it cannot: the flick that picks a
	## chart and the flick that starts it have to be different gestures, or the
	## first one to arrive starts whatever happened to be highlighted.
	if event.source != "imu" or not event.has_direction():
		return
	match event.vertical():
		1:
			_move(-1)
		-1:
			_move(1)
		_:
			_start(_selected)


func _move(step: int) -> void:
	_selected = posmod(_selected + step, _buttons.size())
	_buttons[_selected].grab_focus()


func _start(index: int) -> void:
	var map: Dictionary = MAPS[index]
	var game: Node = (load("res://node_2d.tscn") as PackedScene).instantiate()
	# Set before it enters the tree, so its _ready() loads the chart that was
	# chosen here rather than the default on the exported property. This is why
	# the scene is instanced by hand instead of change_scene_to_file(), which
	# offers nowhere to put anything before _ready() runs.
	game.beatmap_path = String(map["beatmap"])
	game.audio_path = String(map["audio"])
	game.video_path = String(map["video"])
	game.song_title = String(map["title"])

	var tree := get_tree()
	var previous := tree.current_scene
	tree.root.add_child(game)
	tree.current_scene = game
	previous.queue_free()
