extends Node
## Receives IMU flicks from the host-side bridge and feeds them to TapInputBus.
##
## The bridge (dashboard/game_bridge.py) talks to the board over USB serial or
## over WiFi, runs the flick detector, and posts one JSON datagram per flick to
## this port. Godot only ever sees UDP on localhost, which is deliberate:
##
##   * Godot has no serial port API at all, so a COM port could only be reached
##     through a GDExtension binary built per platform. The bridge is what
##     makes "USB or WiFi" a choice the player gets to make without the game
##     needing either.
##   * Detection stays in one place. The bridge runs the same FlickDetector the
##     dashboard displays, so a flick tuned on the dashboard behaves
##     identically here, and there is no second implementation to drift.
##
## Nothing here blocks or retries. If the bridge is not running, the game plays
## on mouse, touch and keyboard exactly as before -- an absent bridge is a
## normal state, not an error.
##
## Command line:
##   --imu-port=3334     listen somewhere else (must match the bridge)
##   --no-imu            do not open a socket at all

## Emitted for every flick accepted, after it has been forwarded to
## TapInputBus. Carries the raw record so a debug overlay can show it.
signal flick_received(record: Dictionary)

## Emitted when the link goes up or down, for anything that shows link state.
signal link_changed(up: bool)

const DEFAULT_PORT := 3334
const WIRE_VERSION := 1

## How long without a datagram before the link is treated as down. The bridge
## sends a status record every second, so anything past a few seconds means it
## has gone away rather than that the player is holding still.
const LINK_TIMEOUT_S := 3.0

var enabled: bool = true
var port: int = DEFAULT_PORT

## True while datagrams are arriving. Drives nothing here; exposed for UI.
var link_up: bool = false
## Last thing worth telling a human, e.g. why the socket would not open.
var status_text: String = "not started"
## Board-side sample rate the bridge last reported, or 0.0.
var board_rate_hz: float = 0.0
var flicks_received: int = 0
var last_bearing_deg: float = NAN
var last_strength: float = 0.0
## Detection lag on the last flick, in ms — how far back scoring reached.
var last_lag_ms: float = 0.0

var _socket := PacketPeerUDP.new()
var _open: bool = false
var _last_packet_ms: int = 0
var _last_seq: int = -1
var _dropped: int = 0
var _warned_version: bool = false


func _ready() -> void:
	_read_command_line()
	if not enabled:
		status_text = "disabled with --no-imu"
		print("[imu] ", status_text)
		return
	_open_socket()


func _read_command_line() -> void:
	for argument in OS.get_cmdline_args():
		if argument == "--no-imu":
			enabled = false
		elif argument.begins_with("--imu-port="):
			var value := argument.get_slice("=", 1)
			if value.is_valid_int():
				port = int(value)


func _open_socket() -> void:
	# Bound to loopback on purpose. The bridge runs on this machine, and
	# binding the wildcard address would leave a port open to the network that
	# feeds straight into gameplay input.
	var error := _socket.bind(port, "127.0.0.1")
	if error != OK:
		_open = false
		status_text = "could not bind UDP %d (error %d)" % [port, error]
		push_warning("[imu] " + status_text + " -- another copy of the game, or "
			+ "another program, already has that port. --imu-port= moves it.")
		return
	_open = true
	status_text = "listening on 127.0.0.1:%d" % port
	print("[imu] ", status_text, " -- start dashboard/game_bridge.py to feed it")


func _exit_tree() -> void:
	if _open:
		_socket.close()
		_open = false


func _process(_delta: float) -> void:
	if not _open:
		return

	while _socket.get_available_packet_count() > 0:
		var raw := _socket.get_packet()
		if raw.size() == 0:
			continue
		_handle_datagram(raw.get_string_from_utf8())

	# Fall back to "down" when the bridge stops talking, so the UI does not go
	# on claiming a link that ended when someone unplugged the board.
	if link_up and Time.get_ticks_msec() - _last_packet_ms > int(LINK_TIMEOUT_S * 1000.0):
		link_up = false
		status_text = "bridge stopped sending"
		link_changed.emit(false)


func _handle_datagram(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		push_warning("[imu] ignoring a datagram that is not a JSON object: " + text)
		return
	var record: Dictionary = parsed

	var version := int(record.get("v", 0))
	if version != WIRE_VERSION and not _warned_version:
		_warned_version = true
		push_warning("[imu] bridge speaks wire version %d, this build expects %d. "
			% [version, WIRE_VERSION]
			+ "Records are being read anyway; update whichever side is older.")

	_last_packet_ms = Time.get_ticks_msec()
	if not link_up:
		link_up = true
		link_changed.emit(true)

	match String(record.get("type", "")):
		"flick":
			_handle_flick(record)
		"hello":
			board_rate_hz = float(record.get("rate_hz", 0.0))
			status_text = "bridge on %s via %s" % [
				record.get("target", "?"), record.get("transport", "?")]
			print("[imu] ", status_text)
		"status":
			if bool(record.get("connected", true)):
				board_rate_hz = float(record.get("rate_hz", 0.0))
				status_text = "%.0f Hz from the board" % board_rate_hz
			else:
				status_text = String(record.get("detail", "board disconnected"))
				push_warning("[imu] " + status_text)
		"bye":
			link_up = false
			status_text = "bridge exited"
			link_changed.emit(false)


func _handle_flick(record: Dictionary) -> void:
	if not record.has("bearing"):
		return

	# A datagram can be lost or reordered; neither is worth dropping a flick
	# over, but a running count of the gaps is worth having when someone asks
	# why WiFi feels worse than the cable.
	var seq := int(record.get("seq", -1))
	if seq >= 0:
		if _last_seq >= 0 and seq > _last_seq + 1:
			_dropped += seq - _last_seq - 1
		_last_seq = maxi(_last_seq, seq)

	var bearing := float(record["bearing"])
	var strength := clampf(float(record.get("strength", 1.0)), 0.0, 1.0)

	# How stale this flick already was when the bridge sent it, plus the trip
	# to here. The trip is loopback and sub-millisecond; the detection lag is
	# tens of milliseconds and is the part that matters.
	var lag := maxf(0.0, float(record.get("lag_ms", 0.0)))

	flicks_received += 1
	last_bearing_deg = bearing
	last_strength = strength
	last_lag_ms = lag

	TapInputBus.report_direction("imu", bearing_to_game_angle(bearing),
		strength, lag)
	flick_received.emit(record)


## Convert the bridge's bearing into the angle convention the game draws in.
##
## The bridge reports degrees clockwise from straight up, which is how a person
## describes a movement they made with their hand. The game measures angles
## counter-clockwise from screen right -- that is what _vec(a) = (cos a, -sin a)
## in node_2d.gd draws, and what sector_angle is written in.
##
## The two run in opposite directions and start a quarter turn apart, so the
## conversion is a reflection and a rotation at once: 90 - bearing. Straight up
## (bearing 0) becomes 90, right (bearing 90) becomes 0, down becomes 270.
##
## dashboard/tests/test_gamebridge.py restates this formula and checks it
## against the game's real lane layout, because a mistake here is invisible --
## it does not throw, it just puts every flick in the wrong lane.
static func bearing_to_game_angle(bearing_deg: float) -> float:
	return fposmod(90.0 - bearing_deg, 360.0)


## Datagrams the sequence numbers say went missing. Non-zero over WiFi is
## normal and costs at most a flick each; non-zero over serial is a bug.
func dropped_count() -> int:
	return _dropped


## One line describing the link, for a debug overlay.
func debug_line() -> String:
	if not enabled:
		return "IMU off"
	if not _open:
		return "IMU: " + status_text
	if not link_up:
		return "IMU: waiting for bridge on :%d" % port
	return "IMU: %s  %d flicks%s" % [
		status_text, flicks_received,
		"  %d lost" % _dropped if _dropped > 0 else ""]
