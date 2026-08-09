extends Control

const TapEvent = preload("res://TapInputBus.gd").TapEvent

## The charts on offer, in the order they are listed.
##
## A map is four paths and a caption rather than a scene of its own: the
## gameplay scene takes all of it as exported properties, so adding a chart is
## adding an entry here and dropping a JSON file next to it. An empty audio or
## video path means the chart genuinely has neither, which is a different thing
## from a file that failed to load and is treated as such.
const MAPS := [
	{
		"title": "Bad Apple!!",
		"caption": "138 BPM   141 notes",
		"beatmap": "res://badapple_hex.json",
		"audio": "res://Bad-Apple-Cut-Audio.ogg",
		"video": "res://Bad-Apple-Cut-Video.ogv",
	},
	{
		"title": "Flick Test",
		"caption": "100 BPM   32 notes   no music, no video",
		"beatmap": "res://test_flicks.json",
		"audio": "",
		"video": "",
	},
]

@onready var maps_box: VBoxContainer = $Maps
@onready var hint: Label = $Hint

var _buttons: Array[Button] = []
var _selected: int = 0


func _ready() -> void:
	for index in MAPS.size():
		var map: Dictionary = MAPS[index]
		var button := Button.new()
		button.text = "%s\n%s" % [map["title"], map["caption"]]
		button.custom_minimum_size = Vector2(360, 92)
		button.add_theme_font_size_override("font_size", 22)
		button.pressed.connect(_start.bind(index))
		# Moving the highlight with the board has to move the keyboard's focus
		# too, or the two disagree about what ENTER would start.
		button.focus_entered.connect(func() -> void: _selected = index)
		maps_box.add_child(button)
		_buttons.append(button)
	_buttons[0].grab_focus()

	TapInputBus.tap.connect(_on_tap)
	if ImuInput.link_up:
		hint.text = "flick up or down to choose, sideways to start"


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
