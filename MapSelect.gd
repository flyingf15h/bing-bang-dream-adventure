extends Control

@onready var map_button: Button = $MapButton


func _ready() -> void:
	map_button.pressed.connect(_on_map_confirmed)
	map_button.grab_focus()


func _on_map_confirmed() -> void:
	get_tree().change_scene_to_file("res://node_2d.tscn")
