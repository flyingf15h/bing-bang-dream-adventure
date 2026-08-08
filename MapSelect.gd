extends Control

const TapEvent = preload("res://TapInputBus.gd").TapEvent

@onready var map_button: Button = $MapButton
@onready var hint: Label = $Hint


func _ready() -> void:
	map_button.pressed.connect(_on_map_confirmed)
	map_button.grab_focus()

	# One map, so any flick confirms it. When there is more than one, this is
	# where left/right would move the selection and up would confirm --
	# TapEvent.vertical() already distinguishes them.
	TapInputBus.tap.connect(_on_tap)
	if ImuInput.link_up:
		hint.text = "flick the board, click the map, or press ENTER to start"


func _on_tap(event: TapEvent) -> void:
	if event.source != "imu":
		return
	_on_map_confirmed()


func _on_map_confirmed() -> void:
	get_tree().change_scene_to_file("res://node_2d.tscn")
