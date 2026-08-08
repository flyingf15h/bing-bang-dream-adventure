extends Node
## Central hub for rhythm "tap" events.
##
## Nothing in gameplay should read mouse/touch/IMU input directly. Instead,
## every input source calls report_tap() here, and gameplay listens to the
## `tap` signal. That way, once the IMU is finished, it plugs in as just
## another caller of report_tap() (e.g. from a serial/UDP listener node)
## without any changes to title screen, gameplay, or scoring code.

signal tap(event: TapEvent)

class TapEvent:
	var source: String
	var timestamp_ms: int
	var screen_position: Vector2
	var strength: float # 0.0-1.0, e.g. IMU impact force; 1.0 for mouse/touch

	func _init(p_source: String, p_screen_position: Vector2, p_strength: float) -> void:
		source = p_source
		timestamp_ms = Time.get_ticks_msec()
		screen_position = p_screen_position
		strength = p_strength


func report_tap(source: String, screen_position: Vector2 = Vector2.ZERO, strength: float = 1.0) -> void:
	tap.emit(TapEvent.new(source, screen_position, strength))
