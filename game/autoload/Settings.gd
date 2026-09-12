extends Node
## Every setting the game has: IMU tuning, audio, gameplay assist -- and the
## one place that sends the IMU half of it to the bridge.
##
## Kinds of setting live here and they behave differently, which is worth
## being clear about because the difference is the whole design:
##
##   * **Detection** settings -- the front axis, the thresholds, the swing and
##     margin floors -- belong to the bridge, because that is where the
##     detector runs. This node does not act on them. It remembers them, sends
##     them, and then displays whatever the bridge says it is actually running.
##     `applied` is that echo, and it is what the panel shows: a value the game
##     believes but the bridge never received would be a lie in exactly the
##     situation someone is using the panel to escape from.
##
##   * **Display** settings -- the arrow, and whether anything but a scoring
##     hit gets colour -- are the game's own and take effect immediately.
##
##   * **Assist** settings -- how far off a flick may be aimed, how much the
##     timing windows stretch for one, and the audio offset -- are also the
##     game's own, because they are about scoring and feel rather than
##     detection. The bridge decides whether a movement was a flick and which
##     way it went; only the game knows whether there was a note there to hit,
##     or how far the chart's audio lags the speakers it is playing through.
##
##   * **Audio** settings -- the three volume sliders -- act on the audio
##     buses directly (see Step 8); nothing here is sent anywhere.
##
## Everything is saved to user:// on change, so a board tuned once stays tuned
## across runs, and can be exported to a file to move to another machine or to
## keep alongside a particular board. A fresh install with no saved file yet
## imports game/profiles/default.json -- the known-good tuning, committed --
## so day one plays the same as day one hundred.

## Emitted when any value changes, from any source: an edit here, a file that
## was imported, or the bridge reporting what it is running.
signal changed

## Emitted when the bridge answers, so the panel can stop saying "sending".
signal applied_by_bridge(tuning: Dictionary)

## Emitted for the guided helpers, carrying the bridge's reply verbatim.
signal front_suggested(record: Dictionary)
signal rest_measured(record: Dictionary)
signal bias_written(record: Dictionary)

## Emitted for a running calibration sequence (bbda/calseq.py), carrying the
## bridge's cal_state/cal_done record verbatim -- CalibrationWizard is a thin
## renderer over these, not a second place step logic could live.
signal cal_state_received(record: Dictionary)
signal cal_done_received(record: Dictionary)

const SAVE_PATH := "user://imu_settings.cfg"

## Bumped if the meaning of a stored value ever changes. An older file is read
## anyway -- every field is read with a default -- but this is what a migration
## branches on, and what tells a human which build wrote a file they are
## looking at.
##
## 2: the detection floors were taken down to the point where reaching the rate
## threshold is very nearly the whole test, and the game took over resolving
## which lane a flick meant. A file written before that carries floors chosen
## against the old, stricter rules.
## 3: the leniency values were widened again. See `_migrate()`.
## 4: the direction check moved from board axes to gravity; a stored aim
## correction from before that is measuring a mistake that no longer exists.
## 5: this autoload absorbed audio and gameplay settings that used to live
## elsewhere or nowhere (`audio_offset_ms`, `vol_master/music/sfx`), and the
## v1-era dashboard this file's format used to track no longer exists at all.
## Nothing before this version is migrated field by field any more -- see
## `_migrate()` -- because there is no longer a meaningful "what this file
## used to mean" to carry forward across that rewrite.
const FORMAT_VERSION := 5

const FRONT_CHOICES: PackedStringArray = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]

## Detection, sent to the bridge. Defaults match BridgeConfig's, so a fresh
## install and a bridge started by hand agree until somebody changes one.
var front: String = "+X"
var on_threshold_dps: float = 110.0
var min_swing: float = 0.2
var min_margin: float = 0.0
var refractory_ms: float = 200.0
var sector_offset_deg: float = 30.0
var calibrated: bool = true
## How far the rotation has to fall from its own peak before the bridge
## stops measuring the flick and sends it. This is the latency knob: higher
## reports sooner off less of the stroke, lower waits for the swing to die
## away entirely. It does not affect scoring -- the flick carries how late it
## was and the game reaches back by that much -- only how fast the screen
## answers. Matches BridgeConfig.commit_fraction.
var commit_fraction: float = 0.6

## Display, acted on by the game itself.
var show_arrow: bool = true
## When true the arrow only takes a lane's colour for a flick that actually hit
## a note. Live movement, refusals and flicks that hit nothing stay grey, so
## colour on screen means exactly one thing: that scored.
var colour_only_hits: bool = true
## When true the only thing ever drawn is a movement the detector accepted as a
## flick -- one that was strong enough and clean enough to be sent as an input,
## whether or not there was a note where it went. No live arrow following the
## board between flicks, no rest dot, no mark for a movement that was refused.
##
## Different question from `colour_only_hits`, which is about scoring: that one
## asks "did that count", this one asks "did that register as a flick at all".
## On an unsteady board the live arrow swings the whole time and is exactly
## what is in the way of seeing the answer.
var arrow_flicks_only: bool = false

## --- aim, acted on where a bearing enters the game -------------------------
##
## A correction applied to every bearing the bridge reports, before it becomes
## a direction on screen. Both default to doing nothing, and both are set by
## the panel's direction check rather than by hand.

## Degrees added to every reported bearing, clockwise. This is the fix for
## flicks that are consistently rotated from where they were aimed -- a board
## held at an angle, or a front axis that is right about which axis but leaves
## the whole ring turned.
var bearing_offset_deg: float = 0.0

## Whether to mirror every bearing before the offset.
##
## A rotation cannot fix a reflection, and a reflection is a real failure mode:
## get the handedness of the frame wrong and left and right swap while up and
## down stay put, so three of the six lanes are correct and the check "is it
## rotated" comes out saying no. The direction check tests for this explicitly
## because it is invisible to any single flick.
var bearing_flip: bool = false


## --- assist, acted on by the game's scoring --------------------------------

## How far off a lane's centre a flick may be aimed and still reach the note in
## it, in degrees. The lanes are 60 degrees apart, so anything above 30 lets a
## flick reach past the lane it landed nearest -- which is the point: the
## direction a hand-thrown flick reports is good to a lane, not to a degree,
## and a note 40 degrees away is far more likely to be the one meant than no
## note at all.
##
## 30 is the strict behaviour: only ever the nearest lane. The default 75
## reaches a lane and a quarter either side, so four of the six lanes are
## candidates for any one flick -- which sounds reckless and is not, because
## which of them wins is decided by AIM_COST_MS_PER_DEG in Gameplay.gd: a note
## further from where the flick pointed has to be correspondingly better timed
## to take it. The tolerance says what is *reachable*; that says what is
## *likelier*. 100 is the ceiling, past which opposite sides of the ring start
## reaching each other and the direction stops meaning anything at all.
var lane_tolerance_deg: float = 75.0

## What the timing windows are multiplied by for a flick, against the same
## windows a key press is judged on.
##
## A flick is not an instant. The bridge times it from the peak of the rotation
## and subtracts the detection lag, but the peak of a gesture a person made
## with their whole arm is a broader thing than the moment a key went down, and
## what is left over is jitter of a few tens of milliseconds that no calibration
## removes. Stretching the windows for flicks and not for keys is what keeps
## that from reading as bad play.
var timing_scale: float = 2.8

## Milliseconds to shift the chart's audio against the notes, positive meaning
## the audio is delayed. Every audio pipeline from the sound card to whatever
## the player is listening on adds some lag that the video/note timing does
## not share, and this is what cancels it out -- there is no way to measure it
## from in here, so it is a player-set offset, not a detected one. Previously
## a Gameplay.gd-only @export with nowhere to persist between runs; living
## here means it survives a restart like every other tuning value.
var audio_offset_ms: float = -50.0


## --- audio, acted on directly through the audio buses -----------------------
##
## Linear 0..1, applied as set_bus_volume_db(linear_to_db(v)) with the bus
## muted outright at 0 -- see Step 8 -- because linear_to_db(0.0) is -inf and
## some audio drivers handle that worse than an explicit mute.
var vol_master: float = 1.0
var vol_music: float = 1.0
var vol_sfx: float = 1.0

## Whether the debug panel is open. Stored, because someone tuning a board
## across several runs should not have to reopen it every time.
var panel_open: bool = false

## What the bridge last said it was running, or empty before it has said
## anything. Never written by the panel -- only by the bridge's echo.
var applied: Dictionary = {}

## Which tuning values the player has deliberately changed, as a set of keys.
##
## This is what decides who wins when the two disagree at connect time. A
## bridge started as `run_bridge.py --front +Y` is making a statement, and so
## is somebody who set the front axis in the panel last week; the difference is
## that only the second one is recorded here. Untouched settings are left to
## the bridge, so its flags mean what they say -- and touched ones are re-sent,
## so a tuning survives the bridge being restarted.
var edited: Dictionary = {}

var _socket := PacketPeerUDP.new()
var _connected_port: int = 0
var _loaded: bool = false


func _ready() -> void:
	load_settings()
	changed.connect(_apply_audio_buses)
	_apply_audio_buses()
	# The bridge may already be running, or may start later; either way the
	# first thing to do on hearing from it is to send it what is stored here,
	# so a saved tuning survives a bridge restart without anybody re-entering
	# it. ImuInput reports every hello, including reconnections.
	ImuInput.link_changed.connect(func(up: bool) -> void:
		if up:
			push_edited_to_bridge())


## Pushed straight to the audio buses rather than read by whoever plays a
## sound -- volume is the one setting here with an engine-level home to live
## in, and going through it means a change takes effect immediately for
## audio already playing, not just for the next stream that starts.
func _apply_audio_buses() -> void:
	_apply_bus_volume("Master", vol_master)
	_apply_bus_volume("Music", vol_music)
	_apply_bus_volume("SFX", vol_sfx)


func _apply_bus_volume(bus_name: String, linear: float) -> void:
	var idx := AudioServer.get_bus_index(bus_name)
	if idx < 0:
		return
	AudioServer.set_bus_volume_db(idx, linear_to_db(linear))
	# linear_to_db(0.0) is -inf, and some audio drivers handle that worse than
	# an explicit mute -- so zero is muted outright rather than trusted to a
	# volume low enough that it should not matter.
	AudioServer.set_bus_mute(idx, linear <= 0.001)


## --- what the bridge is told ----------------------------------------------

func tuning() -> Dictionary:
	return {
		"front": front,
		"on_threshold_dps": on_threshold_dps,
		"min_swing": min_swing,
		"min_margin": min_margin,
		"refractory_ms": refractory_ms,
		"sector_offset_deg": sector_offset_deg,
		"calibrated": calibrated,
		"commit_fraction": commit_fraction,
	}


## What the game's own scoring reads. Deliberately not part of `tuning()`:
## nothing here is ever sent to the bridge, because the bridge cannot act on
## it -- it does not know where the notes are.
func assist() -> Dictionary:
	return {
		"lane_tolerance_deg": lane_tolerance_deg,
		"timing_scale": timing_scale,
		"audio_offset_ms": audio_offset_ms,
	}


## Volume for the three buses. Not sent anywhere -- see `set_audio()`.
func audio() -> Dictionary:
	return {
		"vol_master": vol_master,
		"vol_music": vol_music,
		"vol_sfx": vol_sfx,
	}


## Per-board aim corrections, keyed by the hand the bridge named.
##
## `bearing_offset_deg` and `bearing_flip` above are this game's single board --
## the "" hand -- and stay where they are so a one-board setup reads and writes
## exactly the file it always did. A second board gets its own entry here.
##
## Two mountings are two different errors. The boards are held in two hands, one
## may be a different unit with a different front axis, and a correction fitted
## against one of them is wrong for the other by however differently it happens
## to sit. Sharing one offset would mean the direction check could only ever be
## right about whichever board was checked last.
var hand_aim: Dictionary = {}


## The aim correction in force for one board, in degrees clockwise.
func aim_offset(hand: String = "") -> float:
	if hand == "" or not hand_aim.has(hand):
		return bearing_offset_deg
	return float(hand_aim[hand].get("offset", 0.0))


## Whether that board's bearings are mirrored before the offset.
func aim_flip(hand: String = "") -> bool:
	if hand == "" or not hand_aim.has(hand):
		return bearing_flip
	return bool(hand_aim[hand].get("flip", false))


## Set the aim correction the direction check worked out, and save it.
func set_aim(offset_deg: float, flip: bool, hand: String = "") -> void:
	if hand == "":
		bearing_offset_deg = fposmod(offset_deg, 360.0)
		bearing_flip = flip
	else:
		hand_aim[hand] = {"offset": fposmod(offset_deg, 360.0), "flip": flip}
	save_settings()
	changed.emit()


## Back to no correction at all, which is also what a fresh install has.
func clear_aim(hand: String = "") -> void:
	if hand == "":
		hand_aim.clear()
	set_aim(0.0, false, hand)


## True when a correction is in force, so a display can say so rather than
## leaving somebody to wonder why the raw bearing and the lane disagree.
func aim_corrected(hand: String = "") -> bool:
	return aim_flip(hand) or absf(angle_difference(
		0.0, deg_to_rad(aim_offset(hand)))) > 0.001


## Change one assist value and save. The counterpart of `set_tuning()` for the
## settings that stay in the game; there is no `edited` bookkeeping because
## there is no bridge to disagree with.
func set_assist(key: String, value: float) -> void:
	if not assist().has(key):
		push_warning("[imu] set_assist called with unknown key " + key)
		return
	set(key, value)
	_clamp_assist()
	save_settings()
	changed.emit()


## The audio counterpart of `set_assist()`: acts immediately, since a volume
## slider has no bridge to disagree with either.
func set_audio(key: String, value: float) -> void:
	if not audio().has(key):
		push_warning("[imu] set_audio called with unknown key " + key)
		return
	set(key, clampf(value, 0.0, 1.0))
	save_settings()
	changed.emit()


func _read_assist(values: Dictionary) -> void:
	lane_tolerance_deg = float(values.get("lane_tolerance_deg", lane_tolerance_deg))
	timing_scale = float(values.get("timing_scale", timing_scale))
	audio_offset_ms = float(values.get("audio_offset_ms", audio_offset_ms))
	_clamp_assist()


func _read_audio(values: Dictionary) -> void:
	vol_master = clampf(float(values.get("vol_master", vol_master)), 0.0, 1.0)
	vol_music = clampf(float(values.get("vol_music", vol_music)), 0.0, 1.0)
	vol_sfx = clampf(float(values.get("vol_sfx", vol_sfx)), 0.0, 1.0)


## Kept inside the range the sliders offer, wherever a value came from. A file
## carrying a tolerance of 300 degrees would make every flick hit every note,
## which is not leniency -- it is scoring nothing at all.
func _clamp_assist() -> void:
	lane_tolerance_deg = clampf(lane_tolerance_deg, 30.0, 100.0)
	timing_scale = clampf(timing_scale, 1.0, 4.0)
	audio_offset_ms = clampf(audio_offset_ms, -300.0, 300.0)


func push_to_bridge() -> void:
	## Send every detection setting. Fire and forget: the reply is what counts,
	## and it arrives on the normal datagram path as a `config` record.
	var message := tuning()
	message["cmd"] = Wire.CMD_SET
	_send(message)


func push_edited_to_bridge() -> void:
	## Send only what the player has deliberately changed.
	##
	## Used when the bridge appears, so that starting it with a flag is not
	## silently undone by a stored value nobody has touched -- while a setting
	## somebody did choose still survives a bridge restart.
	if edited.is_empty():
		request_config()
		return
	var message := {"cmd": Wire.CMD_SET}
	var all := tuning()
	for key in edited:
		if all.has(key):
			message[key] = all[key]
	_send(message)


## Change one tuning value: records it as deliberate, saves, and returns it so
## callers can chain. Everything that edits tuning should go through here --
## a value set directly is one the bridge will not be told about at reconnect.
func set_tuning(key: String, value: Variant) -> void:
	if not tuning().has(key):
		push_warning("[imu] set_tuning called with unknown key " + key)
		return
	set(key, value)
	edited[key] = true
	save_settings()


func request_config() -> void:
	_send({"cmd": Wire.CMD_GET})


## Ask one board to watch the next flick and say which front axis would put
## it where the player says they aimed. `expect_bearing` is degrees clockwise
## from up, the convention a person naming a direction uses: 0 up, 90 right.
##
## Per-hand, like every command below it here: the front axis is a fact
## about one board's mounting, and arming both at once means whichever board
## gets flicked next answers for both of them.
func learn_front(expect_bearing: float, hand: String = "") -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_LEARN_FRONT, "expect_bearing": expect_bearing})


func measure_rest(seconds: float = 2.0, hand: String = "") -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_MEASURE_REST, "seconds": seconds})


func write_bias(hand: String = "") -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_WRITE_BIAS})


## --- calibration sequence (bbda/calseq.py) ----------------------------------

func cal_start(hand: String, kind: String = "full") -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_CAL_START, "kind": kind})


func cal_advance(hand: String) -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_CAL_ADVANCE})


func cal_cancel(hand: String) -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_CAL_CANCEL})


func cal_save(hand: String) -> void:
	send_to_hand(hand, {"cmd": Wire.CMD_CAL_SAVE})


func _send(message: Dictionary) -> void:
	## To every bridge that has announced itself, not just to one.
	##
	## Two boards are two bridges on two control ports, and everything sent
	## through here is detection tuning -- thresholds, margins, the sector
	## layout -- which describes what a flick *is* and so has to be the same for
	## both. Sending to one would leave the other running whatever it was
	## started with, and the panel would be showing one board's settings while
	## half the flicks on screen came from the other.
	##
	## Anything that is a fact about *one* board rather than about detection in
	## general -- the front axis, switching its transport, running its
	## calibration -- goes through `send_to_hand()` instead.
	for port in control_ports():
		_send_to_port(port, message)


## Send to one board's control port only. Sending a transport switch or a
## calibration command to every bridge would be telling the second board to
## do what the first one was just asked to.
func send_to_hand(hand: String, message: Dictionary) -> void:
	_send_to_port(int(_hand_ports.get(hand, control_port())), message)


func _send_to_port(port: int, message: Dictionary) -> void:
	if port != _connected_port:
		_socket.close()
		if _socket.connect_to_host("127.0.0.1", port) != OK:
			return
		_connected_port = port
	_socket.put_packet(JSON.stringify(message).to_utf8_buffer())


## Ask one board to reopen on a different transport, live -- the Controllers
## tab's USB/WiFi toggle. Leave `host` empty to go back to USB (the bridge
## auto-finds the port); set it (with `udp_port` if not the board's default)
## to move to WiFi.
func switch_transport(hand: String, host: String = "", udp_port: int = 3333) -> void:
	var message := {"cmd": Wire.CMD_TRANSPORT}
	if host != "":
		message["host"] = host
		message["udp_port"] = udp_port
	send_to_hand(hand, message)


## Every bridge control port the game has heard from, lowest first.
##
## Before any `config` record has arrived the convention holds -- one above the
## port the game listens on, whatever that was moved to -- so a panel opened
## before the bridge starts still reaches it.
func control_ports() -> Array:
	if _control_ports.is_empty():
		return [ImuInput.port + 1]
	var ports: Array = _control_ports.keys()
	ports.sort()
	return ports


## The first bridge's control port. Kept for anything that just wants "the"
## port to show; `control_ports()` is what sending uses.
func control_port() -> int:
	return int(control_ports()[0])


## --- the bridge's echo -----------------------------------------------------

## Control ports seen in `config` records, as a set. One entry per board.
var _control_ports: Dictionary = {}

## Control port for each hand that has announced itself, keyed by hand
## ("" for a single untagged board). What `send_to_hand()` reaches for.
var _hand_ports: Dictionary = {}


func note_bridge_config(record: Dictionary) -> void:
	## Take what the bridge says it is running as the truth.
	##
	## Adopted into the stored values, not just displayed. The bridge is
	## authoritative -- it may have been started with flags that differ from
	## what is saved here -- and a panel that showed one number while the
	## detector used another is the exact confusion this whole screen exists
	## to remove.
	applied = record.duplicate()
	if record.has("control_port"):
		var port := int(record["control_port"])
		_control_ports[port] = true
		_hand_ports[String(record.get("hand", ""))] = port
	# The front axis is a property of one board's mounting, so with two boards
	# it must not be adopted from whichever of them spoke last -- that would
	# hand the second board's mounting to the first every time it reconnected.
	# With a single untagged board it is still adopted, exactly as before.
	var record_hand := String(record.get("hand", ""))
	# Note what this does *not* do: mark anything as edited. Adopting the
	# bridge's own values must not make them look like choices the player made,
	# or the first connection would freeze whatever flags that bridge happened
	# to be started with into this file for ever.
	var before := tuning()
	if record_hand == "" and record.has("front") 			and String(record["front"]) in FRONT_CHOICES:
		front = String(record["front"])
	on_threshold_dps = float(record.get("on_threshold_dps", on_threshold_dps))
	min_swing = float(record.get("min_swing", min_swing))
	min_margin = float(record.get("min_margin", min_margin))
	refractory_ms = float(record.get("refractory_ms", refractory_ms))
	commit_fraction = float(record.get("commit_fraction", commit_fraction))
	sector_offset_deg = float(record.get("sector_offset_deg", sector_offset_deg))
	calibrated = bool(record.get("calibrated", calibrated))
	applied_by_bridge.emit(record)
	if before != tuning():
		save_settings()
	changed.emit()


## --- storage ---------------------------------------------------------------

func load_settings() -> void:
	var file := ConfigFile.new()
	if file.load(SAVE_PATH) != OK:
		_loaded = true
		# A fresh install has nothing to migrate and nothing to guess at --
		# import the known-good tuning committed alongside the game, the same
		# way a player's own exported file would be imported.
		var error := import_from("res://profiles/default.json")
		if error != "":
			push_warning("[imu] could not load the default profile: " + error)
		return
	_read_from(file)
	# Before _migrate(), not after: it may call import_from(), which calls
	# save_settings(), which refuses to write anything while this is false --
	# a reset that never reaches disk would just repeat itself, silently,
	# every single launch.
	_loaded = true
	_migrate(int(file.get_value("imu", "format", 1)))
	changed.emit()


func _migrate(stored_format: int) -> void:
	## This is a breaking release: the wire format, the detector's tuning
	## surface and this file's own schema (Settings absorbing audio and
	## gameplay settings that used to live elsewhere or nowhere) all changed
	## underneath it at once, so there is no longer a meaningful field-by-field
	## story for "what an old value used to mean". A file from before
	## FORMAT_VERSION is reset to the current defaults outright, the same
	## defaults `res://profiles/default.json` carries, rather than picked apart
	## version by version -- and said so, so this does not look like settings
	## quietly vanishing.
	if stored_format >= FORMAT_VERSION:
		return
	var error := import_from("res://profiles/default.json")
	print("[imu] settings file was format %d; this build is %d and does not "
		% [stored_format, FORMAT_VERSION]
		+ "migrate older files field by field -- reset to the current "
		+ "defaults instead."
		+ ("" if error == "" else " (%s)" % error))


func save_settings() -> void:
	if not _loaded:
		return          # do not write defaults over a file still being read
	var file := ConfigFile.new()
	file.set_value("imu", "format", FORMAT_VERSION)
	for key in tuning():
		file.set_value("imu", key, tuning()[key])
	file.set_value("imu", "edited", edited.keys())
	file.set_value("display", "show_arrow", show_arrow)
	file.set_value("display", "colour_only_hits", colour_only_hits)
	file.set_value("display", "arrow_flicks_only", arrow_flicks_only)
	file.set_value("display", "panel_open", panel_open)
	for key in assist():
		file.set_value("assist", key, assist()[key])
	file.set_value("aim", "bearing_offset_deg", bearing_offset_deg)
	file.set_value("aim", "bearing_flip", bearing_flip)
	file.set_value("aim", "hand_aim", hand_aim)
	for key in audio():
		file.set_value("audio", key, audio()[key])
	file.save(SAVE_PATH)


func _read_from(file: ConfigFile) -> void:
	front = String(file.get_value("imu", "front", front))
	if not front in FRONT_CHOICES:
		front = "+X"
	on_threshold_dps = float(file.get_value("imu", "on_threshold_dps", on_threshold_dps))
	min_swing = float(file.get_value("imu", "min_swing", min_swing))
	min_margin = float(file.get_value("imu", "min_margin", min_margin))
	refractory_ms = float(file.get_value("imu", "refractory_ms", refractory_ms))
	commit_fraction = float(file.get_value("imu", "commit_fraction", commit_fraction))
	sector_offset_deg = float(file.get_value("imu", "sector_offset_deg", sector_offset_deg))
	calibrated = bool(file.get_value("imu", "calibrated", calibrated))
	edited.clear()
	for key in file.get_value("imu", "edited", []):
		edited[String(key)] = true
	show_arrow = bool(file.get_value("display", "show_arrow", show_arrow))
	colour_only_hits = bool(file.get_value("display", "colour_only_hits", colour_only_hits))
	arrow_flicks_only = bool(file.get_value("display", "arrow_flicks_only", arrow_flicks_only))
	panel_open = bool(file.get_value("display", "panel_open", panel_open))
	_read_assist({
		"lane_tolerance_deg": file.get_value("assist", "lane_tolerance_deg", lane_tolerance_deg),
		"timing_scale": file.get_value("assist", "timing_scale", timing_scale),
		"audio_offset_ms": file.get_value("assist", "audio_offset_ms", audio_offset_ms),
	})
	bearing_offset_deg = fposmod(float(file.get_value("aim", "bearing_offset_deg", bearing_offset_deg)), 360.0)
	bearing_flip = bool(file.get_value("aim", "bearing_flip", bearing_flip))
	hand_aim = file.get_value("aim", "hand_aim", {})
	_read_audio({
		"vol_master": file.get_value("audio", "vol_master", vol_master),
		"vol_music": file.get_value("audio", "vol_music", vol_music),
		"vol_sfx": file.get_value("audio", "vol_sfx", vol_sfx),
	})


## Where the saved file really is, for showing a human. user:// is a real
## directory somewhere unhelpful, and "it saved" is not much use to somebody
## who wants to copy the file to another machine.
func save_location() -> String:
	return ProjectSettings.globalize_path(SAVE_PATH)


## --- export and import -----------------------------------------------------

## Written as JSON rather than as the ConfigFile: a settings file that gets
## mailed to somebody, committed next to a board, or pasted into an issue
## should be readable and editable without this game to open it.
func export_to(path: String) -> String:
	var payload := {
		"format": FORMAT_VERSION,
		"saved": Time.get_datetime_string_from_system(),
		"tuning": tuning(),
		"display": {
			"show_arrow": show_arrow,
			"colour_only_hits": colour_only_hits,
			"arrow_flicks_only": arrow_flicks_only,
		},
		"assist": assist(),
		"aim": {
			"bearing_offset_deg": bearing_offset_deg,
			"bearing_flip": bearing_flip,
		},
		"audio": audio(),
	}
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return "could not write %s (%s)" % [path, error_string(FileAccess.get_open_error())]
	file.store_string(JSON.stringify(payload, "\t"))
	file.close()
	return ""


## Returns "" on success, or a sentence to show the player. Anything the file
## does not mention is left alone rather than reset, so a partial file -- one
## hand-written to carry a single threshold, say -- does what it looks like it
## does.
func import_from(path: String) -> String:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return "could not read %s (%s)" % [path, error_string(FileAccess.get_open_error())]
	# JSON.new().parse() rather than JSON.parse_string(), which pushes an engine
	# error of its own. Being handed the wrong file is an ordinary mistake with
	# an ordinary message, not something that should leave a red parser error in
	# the log for somebody to chase later.
	var reader := JSON.new()
	var text := file.get_as_text()
	file.close()
	if reader.parse(text) != OK or typeof(reader.data) != TYPE_DICTIONARY:
		return "%s is not a settings file" % path.get_file()

	var payload: Dictionary = reader.data
	var version := int(payload.get("format", FORMAT_VERSION))
	if version > FORMAT_VERSION:
		return ("%s was written by a newer build (format %d, this reads %d)"
			% [path.get_file(), version, FORMAT_VERSION])

	var incoming: Dictionary = payload.get("tuning", {})
	if incoming.has("front") and String(incoming["front"]) in FRONT_CHOICES:
		front = String(incoming["front"])
	on_threshold_dps = clampf(float(incoming.get("on_threshold_dps", on_threshold_dps)), 20.0, 1000.0)
	min_swing = clampf(float(incoming.get("min_swing", min_swing)), 0.0, 1.0)
	min_margin = clampf(float(incoming.get("min_margin", min_margin)), 0.0, 0.5)
	refractory_ms = clampf(float(incoming.get("refractory_ms", refractory_ms)), 0.0, 2000.0)
	commit_fraction = clampf(float(incoming.get("commit_fraction", commit_fraction)), 0.2, 0.95)
	sector_offset_deg = fposmod(float(incoming.get("sector_offset_deg", sector_offset_deg)), 360.0)
	calibrated = bool(incoming.get("calibrated", calibrated))

	var display: Dictionary = payload.get("display", {})
	show_arrow = bool(display.get("show_arrow", show_arrow))
	colour_only_hits = bool(display.get("colour_only_hits", colour_only_hits))
	arrow_flicks_only = bool(display.get("arrow_flicks_only", arrow_flicks_only))
	_read_assist(payload.get("assist", {}))
	var aim: Dictionary = payload.get("aim", {})
	bearing_offset_deg = fposmod(float(aim.get("bearing_offset_deg", bearing_offset_deg)), 360.0)
	bearing_flip = bool(aim.get("bearing_flip", bearing_flip))
	_read_audio(payload.get("audio", {}))

	# Importing a file is as deliberate as moving a slider, so what it carries
	# counts as chosen and will be re-sent to a bridge that restarts later.
	for key in incoming:
		if tuning().has(key):
			edited[String(key)] = true

	save_settings()
	push_to_bridge()
	changed.emit()
	return ""


## Back to the defaults at the top of this file, which are also the bridge's.
##
## Clears the record of what was chosen as well as the values, so this really
## is a fresh start: a bridge started with flags gets to keep them from the
## next connection onwards, rather than being overruled by defaults that
## nobody picked.
func reset_to_defaults() -> void:
	front = "+X"
	on_threshold_dps = 110.0
	min_swing = 0.2
	min_margin = 0.0
	refractory_ms = 200.0
	commit_fraction = 0.6
	sector_offset_deg = 30.0
	calibrated = true
	# The assist values go back too. They sit under the same button and are
	# tuning in every sense that matters to the person pressing it -- what is
	# not reset is the display settings, which are about what is on screen
	# rather than about how the board plays.
	lane_tolerance_deg = 75.0
	timing_scale = 2.8
	audio_offset_ms = -50.0
	bearing_offset_deg = 0.0
	bearing_flip = false
	edited.clear()
	save_settings()
	push_to_bridge()
	changed.emit()
