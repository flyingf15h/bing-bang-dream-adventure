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

## Emitted when the bridge reports the board arriving or going away.
##
## Separate from link_changed because they are different failures with
## different fixes, and they are not nested: the bridge can be running
## perfectly while the board is unplugged, which is a live link carrying the
## news that there is nothing behind it.
signal board_changed(connected: bool)

## Emitted for each live motion record, which arrive about 30 times a second
## while the bridge is running. Gameplay ignores these -- they are not inputs
## and nothing is scored from them; they exist so the on-screen arrow can
## follow the board between flicks.
signal motion_updated(game_angle_deg: float, swing_dps: float)

## Emitted when the bridge saw a movement and deliberately did not call it a
## flick. Carries the record, whose `detail` is a sentence fit to show.
signal flick_refused(record: Dictionary)

const DEFAULT_PORT := 3334
const WIRE_VERSION := 1

## How long without a datagram before the link is treated as down. The bridge
## sends a status record every second, so anything past a few seconds means it
## has gone away rather than that the player is holding still.
const LINK_TIMEOUT_S := 3.0

var enabled: bool = true
var port: int = DEFAULT_PORT

## While true, flicks are still announced to everything watching this node but
## are not put on the input bus.
##
## Set by the panel while it is measuring the board -- the direction check and
## the front-axis helper both need the player to throw a flick *at the panel*,
## and on the title screen a flick on the bus starts the game, which ends the
## measurement by leaving the screen it was running on. The distinction is
## real: during a check the board is an instrument, not a controller.
var capture_only: bool = false

## True while datagrams are arriving. Drives nothing here; exposed for UI.
var link_up: bool = false
## True while the bridge says it has a board. False means the bridge is there
## and the board is not -- a cable, a reset, or a sketch that stopped running.
## Only meaningful when link_up; assumed true until told otherwise, since a
## bridge that is talking at all normally has something to talk about.
var board_connected: bool = true
## True when the board is sending at full rate but its readings never change,
## which means the IMU has stopped being read. Nothing can be detected in this
## state, and every other indicator looks healthy -- so it gets said out loud.
var board_stalled: bool = false
## Last thing worth telling a human, e.g. why the socket would not open.
var status_text: String = "not started"
## Board-side sample rate the bridge last reported, or 0.0.
var board_rate_hz: float = 0.0
## How the bridge is reaching the board: "serial", "WiFi", or "demo" when
## there is no board and the flicks are made up. Anything reporting on the
## board's health has to know, or demo mode reads as a broken board.
var transport: String = ""
var flicks_received: int = 0
var last_bearing_deg: float = NAN
var last_strength: float = 0.0
## Detection lag on the last flick, in ms — how far back scoring reached.
var last_lag_ms: float = 0.0

## --- live motion, for the arrow -------------------------------------------
##
## Where the board is being swung right now, in the game's angle convention,
## or NAN before the first motion record. This is not an input: it is not
## quantised to a lane, nothing is scored from it, and a bridge that sends no
## motion records leaves it NAN for ever without affecting play.
var live_angle_deg: float = NAN
## Rotation rate of the part of the movement that swung the board's front,
## in degrees per second. This is what `live_angle_deg` describes the
## direction of, and what `flick_threshold_dps` compares against.
var live_swing_dps: float = 0.0
## The whole rotation rate, swing and twist together. Larger than the swing
## when the board is being rolled, which is a movement with no direction.
var live_dps: float = 0.0
## Rate at which the bridge's detector starts calling a movement a flick, as
## it reported it. Zero until a motion record says otherwise.
var flick_threshold_dps: float = 0.0
## True once a motion record has arrived, so a display can tell "the board is
## still" apart from "this bridge does not send motion".
var motion_supported: bool = false

## Movements the bridge saw and refused, and why the last one was refused.
## A refusal is not a failure of the link -- it is the detector doing its job
## -- but it is the difference between "nothing happened" and "that did not
## count, and here is what to change".
var refused_count: int = 0
var last_refusal: String = ""
var last_refusal_reason: String = ""

## The board's stored gyro bias, as the bridge last read it off the board.
## Shown by the debug panel; nothing here acts on it.
var board_gyro_bias: Array = []

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
		_clear_live_motion()
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
		"motion":
			_handle_motion(record)
		"refused":
			_handle_refusal(record)
		"config":
			# The bridge reporting what it is actually running. Routed to
			# ImuSettings rather than kept here: this node is the input path,
			# and tuning is not part of it.
			ImuSettings.note_bridge_config(record)
		"front_suggestion":
			ImuSettings.front_suggested.emit(record)
		"rest":
			ImuSettings.rest_measured.emit(record)
		"bias_written":
			ImuSettings.bias_written.emit(record)
		"board_cal":
			board_gyro_bias = record.get("gyro_bias", board_gyro_bias)
		"hello":
			board_rate_hz = float(record.get("rate_hz", 0.0))
			transport = String(record.get("transport", ""))
			status_text = "bridge on %s via %s" % [
				record.get("target", "?"), transport]
			print("[imu] ", status_text)
			# The bridge sends hello on every successful open, reconnects
			# included, so this is also how a board that came back says so.
			if not board_connected:
				board_connected = true
				board_changed.emit(true)
		"status":
			var connected := bool(record.get("connected", true))
			# A board can be connected, streaming at full rate, and not
			# measuring anything. That is its own state and it needs its own
			# flag: every other indicator looks healthy while nothing the
			# player does can possibly register.
			var stalled := bool(record.get("stalled", false))
			if stalled != board_stalled:
				board_stalled = stalled
				if stalled:
					push_warning("[imu] " + String(record.get("detail", "board frozen")))
				board_changed.emit(board_connected)
			if stalled:
				status_text = String(record.get("detail", "board frozen"))
			if connected and not stalled:
				board_rate_hz = float(record.get("rate_hz", 0.0))
				status_text = "%.0f Hz from the board" % board_rate_hz
			else:
				board_rate_hz = 0.0
				status_text = String(record.get("detail", "board disconnected"))
				push_warning("[imu] " + status_text)
			if connected != board_connected:
				board_connected = connected
				board_changed.emit(connected)
		"bye":
			link_up = false
			status_text = "bridge exited"
			_clear_live_motion()
			link_changed.emit(false)


## Which note colour each board plays, keyed by the hand the bridge named.
##
## Two boards post to the same port, so a record's `hand` field is the only
## thing separating them. Kept per hand rather than as a single "last flick"
## because with two boards those readouts describe different devices: one board
## can be flicking away while the other is unplugged, and a panel averaging the
## two would show a healthy link for a hand that is not working at all.
var per_hand: Dictionary = {}


## The hands the bridge has actually reported, in the order they first spoke.
## Empty with one untagged board, which is what a single-board setup is.
var hands: Array[String] = []


func _note_hand(record: Dictionary) -> String:
	var hand := String(record.get("hand", ""))
	if hand == "":
		return ""
	if not per_hand.has(hand):
		per_hand[hand] = {"flicks": 0, "refused": 0, "bearing": NAN,
			"connected": true, "last_seen_ms": 0}
		hands.append(hand)
		hands.sort()
	per_hand[hand]["last_seen_ms"] = Time.get_ticks_msec()
	return hand


## True when two boards are in play, so anything showing per-board state knows
## to show two of everything rather than one.
func two_handed() -> bool:
	return hands.size() > 1


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

	var hand := _note_hand(record)
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
	if hand != "":
		per_hand[hand]["flicks"] = int(per_hand[hand]["flicks"]) + 1
		per_hand[hand]["bearing"] = bearing

	if not capture_only:
		# The aim correction is per board: two boards are two mountings held in
		# two hands, and a single offset fitted against one of them is wrong for
		# the other by however differently it happens to sit.
		TapInputBus.report_direction("imu", game_angle_of(bearing, hand),
			strength, lag, hand)
	# Emitted either way, and after the bus on purpose: the panel measuring the
	# board listens here, and gameplay's own judgement of a flick has to have
	# run before anything reacts to it.
	flick_received.emit(record)


func _handle_motion(record: Dictionary) -> void:
	## Live rotation, for anything that draws the board rather than scoring it.
	##
	## Deliberately does not touch TapInputBus. A motion record is not an input
	## -- it says the board is moving, not that the player meant a lane -- and
	## feeding one in would hit a note for every wave of the hand and make the
	## detector's judgement about what counts as a flick irrelevant.
	motion_supported = true
	live_swing_dps = maxf(0.0, float(record.get("swing", 0.0)))
	live_dps = maxf(0.0, float(record.get("dps", live_swing_dps)))
	if record.has("threshold_dps"):
		flick_threshold_dps = maxf(0.0, float(record["threshold_dps"]))
	if record.has("bearing"):
		live_angle_deg = game_angle_of(float(record["bearing"]))
	motion_updated.emit(live_angle_deg, live_swing_dps)


func _handle_refusal(record: Dictionary) -> void:
	## A movement that was seen and not counted.
	##
	## Never forwarded to TapInputBus, obviously: this is the bridge saying it
	## decided *not* to report a flick, and turning that into one would undo
	## every check that led to the decision.
	refused_count += 1
	last_refusal_reason = String(record.get("reason", ""))
	last_refusal = String(record.get("detail", last_refusal_reason))
	flick_refused.emit(record)


func _clear_live_motion() -> void:
	## Forget how the board was moving when the link died.
	##
	## The angle is kept: it is the last direction the board really went, and a
	## display fading an arrow out is better served by it than by NAN. The
	## rates go to zero because "still" is the honest reading of a board that
	## is no longer reporting -- leaving the last one would show an arrow
	## frozen at full stretch as though the player were mid-flick for ever.
	live_swing_dps = 0.0
	live_dps = 0.0


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
##
## Deliberately static and deliberately unaware of the player's aim correction:
## this is the wire convention and nothing else, which is what makes it a thing
## a test can restate. `game_angle_of()` is the one that applies the correction,
## and it is what everything drawing or scoring a flick should call.
static func bearing_to_game_angle(bearing_deg: float) -> float:
	return fposmod(90.0 - bearing_deg, 360.0)


## The bearing the player meant, from the bearing the board reported.
##
## Applies the direction check's findings, in the order they were measured: the
## mirror first, then the rotation. That order matters and is not a convention
## -- a reflection followed by a rotation is a different transform from the
## rotation followed by the reflection, and the check solves for the offset
## *after* deciding whether to flip, so undoing it any other way would apply an
## offset that was never measured.
func corrected_bearing(raw_deg: float, hand: String = "") -> float:
	var bearing: float = -raw_deg if ImuSettings.aim_flip(hand) else raw_deg
	return fposmod(bearing + ImuSettings.aim_offset(hand), 360.0)


## A reported bearing turned into the angle the game draws and scores in, with
## that board's aim correction applied. The one to call from gameplay.
func game_angle_of(raw_bearing_deg: float, hand: String = "") -> float:
	return bearing_to_game_angle(corrected_bearing(raw_bearing_deg, hand))


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
	if not board_connected:
		return "IMU: bridge up, no board -- " + status_text
	if board_stalled:
		return "IMU: board frozen -- replug it"
	return "IMU: %s  %d flicks%s" % [
		status_text, flicks_received,
		"  %d lost" % _dropped if _dropped > 0 else ""]
