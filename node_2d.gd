extends Node2D

const TapEvent = preload("res://TapInputBus.gd").TapEvent

@export var beatmap_path: String = "res://badapple_hex.json"
@export var audio_path:   String = "res://Bad-Apple-Cut-Audio.ogg"
@export var video_path:   String = "res://Bad-Apple-Cut-Video.ogv"
@export var song_title:   String = "Bad Apple!!"

@export var radius: float = 250.0
@export var start_delay: float = 3.2
@export var audio_offset_ms: float = -50.0

@export var video_opacity: float = 0.30
@export var scrim_alpha: float = 0.72

@export var bar_len_centre: float = 12.0
@export var bar_len_edge:   float = 90.0
@export var bar_thickness:  float = 19.0
@export var trail_thickness: float = 40.0
@export var slide_width_scale: float = 1.5
@export var show_numbers: bool = false

## The arrow that shows what the board is doing. Off with the I key, or here
## for a recording where it would only be in the way.
@export var show_imu_arrow: bool = true

@export var win_perfect: float = 45.0
@export var win_near: float = 110.0
@export var slide_grace_ms: float = 120.0


const R_OUTER := Color(1.00, 0.36, 0.72)
const R_INNER := Color(0.60, 0.38, 0.98)
const L_OUTER := Color(0.34, 0.62, 1.00)
const L_INNER := Color(0.34, 0.94, 0.88)
const G_OUTER := Color(1.00, 0.86, 0.42)
const G_INNER := Color(0.97, 0.62, 0.20)

const COL_PERFECT := Color(0.82, 0.74, 1.00)
const COL_EARLY   := Color(0.55, 0.86, 1.00)
const COL_LATE    := Color(1.00, 0.56, 0.82)
const COL_MISS    := Color(0.94, 0.31, 0.44)

const COMBO_LOW  := Color(1.00, 1.00, 1.00)
const COMBO_HIGH := Color(0.72, 0.58, 1.00)
const BAR_PURPLE := Color(0.72, 0.55, 1.00)
const BAR_BLUE   := Color(0.45, 0.78, 1.00)

const SECTOR_KEYS := {
	6: KEY_A, 5: KEY_S, 4: KEY_D,
	1: KEY_J, 2: KEY_K, 3: KEY_L,
}


var sector_angle: Dictionary = {1: 60.0, 2: 0.0, 3: 300.0, 4: 240.0, 5: 180.0, 6: 120.0}
var key_sector: Dictionary = {}

var notes: Array = []
var pairs: Dictionary = {}
var travel: float = 0.8
var travel_base: float = 0.8
var speed_mult: float = 1.0
var travel_target: float = 0.8
var bpm: float = 138.0
var hold_dps: float = 200.0
var chart_end: float = 0.0

var song_time: float = 0.0
var player: AudioStreamPlayer
var video: VideoStreamPlayer
var started := false
var finished := false
var paused := false
var autoplay := false
var centre: Vector2
var load_error: String = ""

var score: int = 0
var combo: int = 0
var best_combo: int = 0
var max_score: int = 1000000
var score_f: float = 0.0
var total_weight: float = 1.0
var counts := {"PERFECT": 0, "EARLY": 0, "LATE": 0, "MISS": 0}
var combo_pop: float = 0.0
var score_shown: float = 0.0

var popups: Array = []
var sparks: Array = []
var ring_flash: Dictionary = {}
var pause_rect: Rect2
var last_seek_at: float = -999.0
var ui_time: float = 0.0
var finish_ui: float = -1.0

var font_bold: FontVariation
var font_heavy: FontVariation
var font_thin: FontVariation
var _vgrad_items: Array = []
var _score_saved: bool = false

## --- the IMU arrows --------------------------------------------------------
## Where each board is pointed and how hard it is being swung, smoothed for
## drawing. None of this is read by scoring: an arrow is a display of an input
## that has already been judged elsewhere, so a bug in here cannot cost a note.
##
## One entry per board, keyed by the hand the bridge named -- "" when there is
## a single board, which is a mouse player's arrow and the arrangement this
## screen had before there were two. With two boards there are two arrows and
## they have to be separate all the way down: they point in different
## directions, they flash at different moments, and one of them is what the
## player is looking for when a blue note goes past untouched.
var imu_arrows: Dictionary = {}


func _arrow(hand: String) -> Dictionary:
	if not imu_arrows.has(hand):
		imu_arrows[hand] = {
			"angle": NAN,       # smoothed heading, game convention
			"reach": 0.0,       # 0..1, swing rate against the flick threshold
			"flash": 0.0,       # 1.0 the moment a flick lands, then decays
			"refused": 0.0,     # 1.0 when a movement was refused, then decays
			"text": "",
			# Whether that board's last flick landed on a note. Everything that
			# is not a scoring hit draws grey, so colour on screen carries
			# exactly one meaning.
			"hit": false,
		}
	return imu_arrows[hand]


func _ready() -> void:
	# ── FIX: enable 2D multisample antialiasing so draw_arc / draw_line /
	#    draw_colored_polygon look as smooth as text.  Without this, all the
	#    circle and note geometry is rendered without AA and looks pixelated.
	get_viewport().msaa_2d = Viewport.MSAA_4X

	centre = get_viewport_rect().size * 0.5
	for s in SECTOR_KEYS:
		key_sector[SECTOR_KEYS[s]] = int(s)

	font_bold = FontVariation.new()
	font_bold.base_font = ThemeDB.fallback_font
	font_bold.variation_embolden = 0.42
	font_heavy = FontVariation.new()
	font_heavy.base_font = ThemeDB.fallback_font
	font_heavy.variation_embolden = 0.72
	font_heavy.spacing_glyph = 1
	# Light weight used across the results screen.
	font_thin = FontVariation.new()
	font_thin.base_font = ThemeDB.fallback_font
	font_thin.variation_embolden = 0.0
	font_thin.spacing_glyph = 1

	_setup_video()
	_load_beatmap()

	player = AudioStreamPlayer.new()
	add_child(player)
	# An empty path is a chart that has no music, not a chart whose music is
	# missing -- the practice map is exactly that. Without the distinction it
	# would come up under a red error saying its silence was a fault. With no
	# stream the clock simply runs off _process's delta and the chart ends at
	# total_duration_ms.
	if audio_path != "":
		var stream = load(audio_path)
		if stream == null:
			load_error = "Cannot load audio %s - Godot needs WAV / OGG / MP3" % audio_path
			push_error(load_error)
		else:
			player.stream = stream
			player.finished.connect(func(): finished = true)

	for i in 52:
		sparks.append(_new_spark(randf()))

	song_time = -start_delay
	set_process(true)
	get_viewport().size_changed.connect(_on_resize)
	TapInputBus.tap.connect(_on_tap)

	# Straight from ImuInput rather than from _on_tap, so the arrow reacts to
	# every flick the board sent -- including the ones _on_tap turns away while
	# paused, in autoplay or before the song starts. Those are exactly the
	# moments someone is checking whether the board is working at all.
	ImuInput.flick_received.connect(_on_imu_flick)
	ImuInput.flick_refused.connect(_on_imu_refused)
	show_imu_arrow = ImuSettings.show_arrow
	ImuSettings.changed.connect(func() -> void: show_imu_arrow = ImuSettings.show_arrow)
	add_child(preload("res://ImuDebugPanel.gd").new())


func _on_resize() -> void:
	centre = get_viewport_rect().size * 0.5
	if video:
		video.size = get_viewport_rect().size


func _setup_video() -> void:
	if video_path == "":
		return          # a chart with no video of its own; the ring is the whole screen
	video = VideoStreamPlayer.new()
	var vs = load(video_path)
	if vs == null:
		push_warning("No video at %s (must be .ogv - Godot cannot play .mp4)" % video_path)
		return
	video.stream = vs
	video.expand = true
	video.size = get_viewport_rect().size
	video.position = Vector2.ZERO
	video.modulate = Color(1, 1, 1, video_opacity)
	video.loop = false
	video.z_index = -100
	add_child(video)


func _load_beatmap() -> void:
	if not FileAccess.file_exists(beatmap_path):
		load_error = "Beatmap not found: %s" % beatmap_path
		push_error(load_error)
		return
	var f := FileAccess.open(beatmap_path, FileAccess.READ)
	var data = JSON.parse_string(f.get_as_text())
	f.close()
	if typeof(data) != TYPE_DICTIONARY:
		load_error = "Beatmap JSON malformed"
		push_error(load_error)
		return

	travel = float(data.get("travel_time_ms", 800)) / 1000.0
	travel_base = travel
	travel_target = travel
	bpm = float(data.get("bpm", 138.0))
	hold_dps = float(data.get("hold_degrees_per_second", 200.0))
	chart_end = float(data.get("total_duration_ms", 0)) / 1000.0
	if data.has("title"):
		song_title = String(data["title"])

	if data.has("sector_angles"):
		var sa: Dictionary = data["sector_angles"]
		sector_angle.clear()
		for k in sa:
			sector_angle[int(String(k))] = float(sa[k])

	for n in data.get("notes", []):
		var hold: float = float(n.get("duration_ms", 0)) / 1000.0
		var sect: int = int(n.get("sector", 1))
		var note := {
			"t": float(n["time_ms"]) / 1000.0,
			"sector": sect,
			"angle": float(sector_angle.get(sect, 0.0)),
			"hand": String(n.get("hand", "left")),
			"hold": hold,
			"bonus": bool(n.get("bonus", false)),
			"pair_id": int(n["pair_id"]) if n.has("pair_id") else -1,
			"sweep": float(n.get("sweep", 1)),
			"state": "wait",
			"judged": "",
			"hit_at": -999.0,
		}
		notes.append(note)
		if note["pair_id"] >= 0:
			if not pairs.has(note["pair_id"]):
				pairs[note["pair_id"]] = []
			pairs[note["pair_id"]].append(note)

	notes.sort_custom(func(a, b): return a["t"] < b["t"])
	_compute_max_score()
	print("loaded %d notes (%d pairs), max score %d"
		% [notes.size(), pairs.size(), max_score])


const SCORE_POOL: float = 1000000.0

func _note_weight(n: Dictionary) -> float:
	var w: float = 1.5 if bool(n["bonus"]) else 1.0
	return w * (2.0 if float(n["hold"]) > 0.0 else 1.0)


func _compute_max_score() -> void:
	total_weight = 0.0
	for n in notes:
		total_weight += _note_weight(n)
	total_weight = maxf(total_weight, 0.001)
	max_score = int(SCORE_POOL)


## Grade colours: D is a vivid blue and each step up shifts toward purple,
## ending on a hot pink-purple for SS+.  The second entry is a lighter tint of
## the same hue, used for the soft glow behind the letter.
const GRADE_RAMPS := {
	"SS+":  [Color(0.78, 0.40, 0.74), Color(0.90, 0.62, 0.86)],
	"SS":   [Color(0.68, 0.42, 0.84), Color(0.83, 0.64, 0.94)],
	"S":    [Color(0.62, 0.44, 0.87), Color(0.78, 0.66, 0.96)],
	"A":    [Color(0.55, 0.46, 0.89), Color(0.73, 0.68, 0.96)],
	"B":    [Color(0.48, 0.50, 0.89), Color(0.67, 0.71, 0.96)],
	"C":    [Color(0.42, 0.54, 0.88), Color(0.62, 0.74, 0.96)],
	"D":    [Color(0.34, 0.58, 0.88), Color(0.57, 0.77, 0.96)],
	"FAIL": [Color(0.85, 0.36, 0.45), Color(0.94, 0.60, 0.66)],
}

func _grade() -> Array:
	var pct: float = clampf(score_f / SCORE_POOL, 0.0, 1.0)
	var key: String = "FAIL"
	if pct >= 0.98:    key = "SS+"
	elif pct >= 0.95:  key = "SS"
	elif pct >= 0.90:  key = "S"
	elif pct >= 0.80:  key = "A"
	elif pct >= 0.70:  key = "B"
	elif pct >= 0.60:  key = "C"
	elif pct >= 0.50:  key = "D"
	var ramp: Array = GRADE_RAMPS[key]
	return [key, ramp[0], pct, ramp[1]]


const RESULT_JACKET: Texture2D = preload("res://assets/jacket_badapple.png")
const CHIBI_BEST: Texture2D = preload("res://assets/bestresult.png")
const CHIBI_GOOD: Texture2D = preload("res://assets/goodresult.png")
const CHIBI_BAD:  Texture2D = preload("res://assets/badresult.png")

const GRADE_CATEGORY := {
	"SS+": "BEST", "SS": "BEST", "S": "BEST",
	"A": "GOOD", "B": "GOOD",
	"C": "BAD", "D": "BAD", "FAIL": "BAD",
}

func _result_art(grade_key: String) -> Texture2D:
	match GRADE_CATEGORY.get(grade_key, "BAD"):
		"BEST": return CHIBI_BEST
		"GOOD": return CHIBI_GOOD
		_: return CHIBI_BAD


func _fmt_score(v: int) -> String:
	var width: int = maxi(str(max_score).length(), 2)
	var d: String = str(maxi(v, 0)).pad_zeros(width)
	var out: String = ""
	var run: int = 0
	for i in range(d.length() - 1, -1, -1):
		out = d[i] + out
		run += 1
		if run % 3 == 0 and i > 0:
			out = "'" + out
	return out


func _process(delta: float) -> void:
	ui_time += delta
	if finished and finish_ui < 0.0:
		finish_ui = ui_time
		_save_result()
	if paused or finished:
		_tick_visuals(delta)
		queue_redraw()
		return

	if not started:
		song_time += delta
		if song_time >= 0.0:
			if player.stream != null:
				player.play()
			if video:
				video.play()
			started = true
	elif player.stream != null:
		if not player.playing:
			finished = true
		else:
			song_time += delta
			var target: float = player.get_playback_position() \
				+ AudioServer.get_time_since_last_mix() \
				- AudioServer.get_output_latency() \
				- audio_offset_ms / 1000.0
			var err: float = target - song_time
			if absf(err) > 0.15:
				song_time = target
			else:
				song_time += err * minf(1.0, delta * 5.0)
			_sync_video()
	else:
		song_time += delta
		if chart_end > 0.0 and song_time > chart_end:
			finished = true

	_update_notes()
	_tick_visuals(delta)
	queue_redraw()


## ── Leaderboard export ──────────────────────────────────────────────────
##
## Every finished run appends one record to scores.json, which leaderboard.html
## polls.  The file is written in two places: user:// always works (including
## in exported builds), and a "public" copy sits next to the project or the
## executable so the web page can fetch it over a local server without the
## player hunting through the OS user-data directory.

func _scores_paths() -> Array:
	var paths: Array = ["user://scores.json"]
	var public_dir: String = ""
	if OS.has_feature("editor"):
		public_dir = ProjectSettings.globalize_path("res://")
	else:
		public_dir = OS.get_executable_path().get_base_dir()
	if public_dir != "":
		paths.append(public_dir.path_join("scores.json"))
	return paths


func _read_score_list(path: String) -> Array:
	if not FileAccess.file_exists(path):
		return []
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		return []
	var parsed = JSON.parse_string(f.get_as_text())
	f.close()
	return parsed if typeof(parsed) == TYPE_ARRAY else []


func _save_result() -> void:
	## Guarded so a run is only ever recorded once, even though _process keeps
	## ticking on the results screen.  _restart() clears the guard.
	if _score_saved:
		return
	_score_saved = true

	var g: Array = _grade()
	var rec := {
		"id": "%d-%04d" % [Time.get_unix_time_from_system(), randi() % 10000],
		"song": song_title,
		"score": score,
		"grade": String(g[0]),
		"pct": float(g[2]),
		"max_combo": best_combo,
		"perfect": int(counts["PERFECT"]),
		"early": int(counts["EARLY"]),
		"late": int(counts["LATE"]),
		"miss": int(counts["MISS"]),
		"at": Time.get_datetime_string_from_system(true),
	}

	for path in _scores_paths():
		var list: Array = _read_score_list(path)
		list.append(rec)
		var f := FileAccess.open(path, FileAccess.WRITE)
		if f == null:
			push_warning("Could not write scores to %s" % path)
			continue
		f.store_string(JSON.stringify(list, "\t"))
		f.close()
		print("saved result to %s" % path)

		# Same data as a plain script.  A page opened straight off disk cannot
		# fetch() a sibling file -- file:// origins are opaque, so both fetch
		# and the File System Access API are unavailable -- but a <script> tag
		# has no such restriction, so leaderboard.html re-injects this on a
		# timer to pick up new runs without the player doing anything.
		var js_path: String = path.get_basename() + ".js"
		var jf := FileAccess.open(js_path, FileAccess.WRITE)
		if jf == null:
			continue
		jf.store_string("window.__scores = %s;\nwindow.__scoresAt = %d;\n"
			% [JSON.stringify(list), Time.get_unix_time_from_system()])
		jf.close()


func _sync_video() -> void:
	if video == null or not video.is_playing():
		return
	if song_time - last_seek_at < 2.0:
		return
	var drift: float = video.stream_position - song_time
	if absf(drift) > 0.35:
		video.stream_position = maxf(0.0, song_time)
		last_seek_at = song_time


func _tick_visuals(delta: float) -> void:
	travel += (travel_target - travel) * minf(1.0, delta * 7.0)
	combo_pop = maxf(0.0, combo_pop - delta * 3.2)
	score_shown += (float(score) - score_shown) * minf(1.0, delta * 9.0)

	for i in range(popups.size() - 1, -1, -1):
		popups[i]["age"] = float(popups[i]["age"]) + delta
		if float(popups[i]["age"]) > 0.7:
			popups.remove_at(i)

	for s in sector_angle:
		if ring_flash.has(s):
			ring_flash[s] = maxf(0.0, float(ring_flash[s]) - delta * 3.5)

	_tick_imu(delta)

	var prog: float = _progress()
	for sp in sparks:
		sp["phase"] = float(sp["phase"]) + delta * float(sp["speed"])
		sp["x"] = float(sp["x"]) + delta * 0.045 * float(sp["drift"])
		if float(sp["x"]) > prog + 0.02 or float(sp["x"]) < 0.0:
			sp["x"] = randf() * maxf(0.02, prog)
			sp["y"] = randf_range(-1.0, 1.0)


## How much of the flick threshold the board has to be swinging through before
## the arrow turns to follow it. Below this the bridge is reporting the shake of
## a hand holding something still, and a direction taken from that is noise --
## an arrow that spun on the spot whenever the board was at rest would read as
## a broken sensor rather than a steady one.
const IMU_STEER_FLOOR: float = 0.08

## What everything that is not a registered hit is drawn in. Kept light enough
## to read over the video, and deliberately not one of the lane colours.
const IMU_GREY := Color(0.78, 0.76, 0.85)

## Fallback for the flick threshold if the bridge has not said what it uses.
## Only reached when motion records arrive without one, which no current bridge
## does; it keeps the arrow scaled sensibly rather than dividing by zero.
const IMU_THRESHOLD_FALLBACK_DPS: float = 150.0


func _tick_imu(delta: float) -> void:
	# Every arrow that exists, not only the boards currently talking: an arrow
	# belonging to a board that has just gone away still has a flash and a
	# refusal to fade out, and stopping the decay would freeze it mid-strike.
	for hand in ImuInput.active_hands():
		_arrow(String(hand))
	for key in imu_arrows:
		_tick_imu_arrow(String(key), imu_arrows[key], delta)


func _tick_imu_arrow(hand: String, arrow: Dictionary, delta: float) -> void:
	arrow["flash"] = maxf(0.0, float(arrow["flash"]) - delta * 2.6)
	# Slower than the flick's, because this one has to be read rather than just
	# noticed: it carries a line of text saying what to change.
	arrow["refused"] = maxf(0.0, float(arrow["refused"]) - delta * 0.55)

	# The reach is how close the swing is to counting as a flick, so a full
	# length arrow means "that would have registered". This is the part that
	# answers the question a player actually asks when a flick does not land --
	# whether they flicked too gently or in the wrong direction -- and the two
	# look completely different here: a short arrow in the right place, or a
	# long one pointing somewhere else.
	var target: float = 0.0
	if ImuInput.link_up and ImuInput.hand_motion(hand) \
			and ImuInput.hand_connected(hand):
		var threshold: float = ImuInput.hand_threshold_dps(hand)
		if threshold <= 0.0:
			threshold = IMU_THRESHOLD_FALLBACK_DPS
		var swing: float = ImuInput.hand_swing_dps(hand)
		target = clampf(swing / threshold, 0.0, 1.0)

		var live: float = ImuInput.hand_angle(hand)
		if target > IMU_STEER_FLOOR and not is_nan(live):
			if is_nan(float(arrow["angle"])):
				arrow["angle"] = live
			else:
				# Round the short way, so a swing across the 0/360 seam does
				# not send the arrow the long way round the ring.
				arrow["angle"] = fposmod(rad_to_deg(lerp_angle(
					deg_to_rad(float(arrow["angle"])), deg_to_rad(live),
					minf(1.0, delta * 20.0))), 360.0)

	arrow["reach"] = float(arrow["reach"]) \
		+ (target - float(arrow["reach"])) * minf(1.0, delta * 14.0)


func _on_imu_flick(record: Dictionary) -> void:
	if not record.has("bearing"):
		return
	var hand := String(record.get("hand", ""))
	var arrow := _arrow(hand)
	# Snap rather than ease. The flick's own bearing is measured over the whole
	# gesture and is what scoring used; the smoothed heading is an approximation
	# of it that lags by a frame or two. Showing the arrow anywhere but on the
	# lane that was hit would make a correct hit look like a mis-aimed one.
	arrow["angle"] = ImuInput.game_angle_of(float(record["bearing"]), hand)
	arrow["flash"] = 1.0
	arrow["refused"] = 0.0


func _on_imu_refused(record: Dictionary) -> void:
	## Show a movement that was seen and not counted.
	##
	## Nothing at all is the one response that cannot be read: a flick that was
	## rejected and a board that is unplugged both produce silence, and a
	## player cannot tell which they are looking at. So a refusal gets its own
	## mark -- deliberately not the flick's, since it did not score.
	var hand := String(record.get("hand", ""))
	var arrow := _arrow(hand)
	if record.has("bearing"):
		arrow["angle"] = ImuInput.game_angle_of(float(record["bearing"]), hand)
	arrow["refused"] = 1.0
	arrow["text"] = String(record.get("detail", ""))
	# Whatever the last flick did, this movement scored nothing, and the
	# colour rules key off that flag rather than off which record was newest.
	arrow["hit"] = false


func _new_spark(x: float) -> Dictionary:
	return {
		"x": x, "y": randf_range(-1.0, 1.0), "phase": randf() * TAU,
		"speed": randf_range(2.0, 5.5), "size": randf_range(1.2, 2.8),
		"drift": randf_range(-1.0, 1.0), "purple": randf() < 0.5,
	}


func _progress() -> float:
	if chart_end <= 0.0:
		return 0.0
	return clampf(song_time / chart_end, 0.0, 1.0)


func _slide_sector_now(n: Dictionary) -> int:
	var elapsed: float = clampf(song_time - float(n["t"]), 0.0, float(n["hold"]))
	return _nearest_sector(float(n["angle"]) + hold_dps * elapsed * float(n["sweep"]))


func _nearest_sector(angle_deg: float) -> int:
	var a: float = fposmod(angle_deg, 360.0)
	var best: int = 1
	var bd: float = 1e9
	for s in sector_angle:
		var d: float = absf(float(sector_angle[s]) - a)
		d = minf(d, 360.0 - d)
		if d < bd:
			bd = d
			best = int(s)
	return best


func _update_notes() -> void:
	for n in notes:
		if String(n["state"]) == "done":
			continue
		var lead: float = float(n["t"]) - song_time

		if autoplay:
			if String(n["state"]) == "wait" and lead <= 0.0:
				if float(n["hold"]) > 0.0:
					n["state"] = "holding"
					_award(n, "PERFECT")
					_popup(n, "PERFECT", COL_PERFECT)
				else:
					_finish(n, "PERFECT")
				continue
			if String(n["state"]) == "holding" and song_time >= float(n["t"]) + float(n["hold"]):
				n["state"] = "done"
				_award(n, "PERFECT")
			continue

		# A note is not given up on until the widest window still open on it has
		# closed. Without this the flick's own window would be a fiction: the
		# note it was reaching for is already marked MISS by the time a flick
		# judged 150 ms late arrives, and widening the window would have changed
		# nothing. Keys are unaffected -- _try_hit() still refuses anything past
		# win_near, so all this moves is the moment the miss is recorded.
		if String(n["state"]) == "wait" and lead < -win_near * _imu_window_scale() / 1000.0:
			_finish(n, "MISS")
			continue

		if String(n["state"]) == "holding":
			var need: int = _slide_sector_now(n)
			var held: bool = Input.is_key_pressed(int(SECTOR_KEYS.get(need, KEY_NONE)))
			if held:
				n["release_at"] = -1.0
			else:
				if float(n.get("release_at", -1.0)) < 0.0:
					n["release_at"] = song_time
				elif song_time - float(n["release_at"]) > slide_grace_ms / 1000.0:
					_finish(n, "MISS")
					continue
			if song_time >= float(n["t"]) + float(n["hold"]):
				n["state"] = "done"
				n["hit_at"] = song_time
				_award(n, "PERFECT")
				_popup(n, "PERFECT", COL_PERFECT)


func _judge_for(err_ms: float, scale: float = 1.0) -> String:
	var a: float = absf(err_ms)
	if a <= win_perfect * scale:
		return "PERFECT"
	if a <= win_near * scale:
		return "EARLY" if err_ms < 0.0 else "LATE"
	return ""


## How much the timing windows stretch for a flick, as the panel has it set.
##
## 1.0 for everything else, and 1.0 for a flick too when there is no bridge --
## the widened window is a concession to a gesture that has to be measured, and
## applying it when nothing is being measured would just make the keyboard
## easier for no reason.
func _imu_window_scale() -> float:
	if not (ImuInput.enabled and ImuInput.link_up):
		return 1.0
	return clampf(ImuSettings.timing_scale, 1.0, 4.0)


## How far apart two angles are, in degrees, the short way round.
func _angle_gap(a_deg: float, b_deg: float) -> float:
	return absf(rad_to_deg(angle_difference(deg_to_rad(a_deg), deg_to_rad(b_deg))))


func _try_hit(sector: int, lag_ms: float = 0.0) -> bool:
	## Returns whether the input landed on a note. The IMU arrow uses that to
	## decide whether to take a lane's colour, so "grey" and "coloured" on
	## screen mean exactly "did not score" and "scored".
	# lag_ms backdates the hit to when the input really happened. A key or a
	# click is known the instant it arrives and passes zero; a flick is only
	# recognised once it is over, so it arrives about half a gesture late and
	# would otherwise be judged against a moment that has already passed.
	var at: float = song_time - lag_ms / 1000.0
	var best: Dictionary = {}
	var best_err: float = 1e9
	for n in notes:
		if String(n["state"]) != "wait" or int(n["sector"]) != sector:
			continue
		var err: float = (at - float(n["t"])) * 1000.0
		if absf(err) <= win_near and absf(err) < absf(best_err):
			best = n
			best_err = err
	if best.is_empty():
		return false
	var verdict: String = _judge_for(best_err)
	if verdict == "":
		return false
	_accept_hit(best, verdict)
	return true


func _accept_hit(n: Dictionary, verdict: String, whole: bool = false) -> void:
	## Award a note that an input has been matched to. The lane that lights up
	## is the note's own, not the one the input was aimed at: with the flick
	## tolerance wide enough to reach a neighbour, those are not always the
	## same, and the note is the one that was actually hit.
	##
	## `whole` settles the note outright instead of opening a hold on it, and is
	## what a flick always passes. A flick is an impulse: it is over before the
	## bridge can even name it, so there is nothing left to hold with and no way
	## to follow a note that sweeps round the ring. Left to open a hold, every
	## drag note a flick landed on would be awarded its first half and then
	## dropped as a MISS the moment the grace ran out -- the input would look
	## like it had scored and the note would still count against the player.
	## So a flick takes the note in one go, for its full value.
	ring_flash[int(n["sector"])] = 1.0
	if float(n["hold"]) > 0.0 and not whole:
		n["state"] = "holding"
		n["release_at"] = -1.0
		_award(n, verdict)
		_popup(n, verdict, _verdict_colour(verdict))
	else:
		_finish(n, verdict, whole)


## Cost of being aimed one degree wide, in milliseconds of timing error.
##
## This is what decides between two notes a flick could plausibly have meant:
## the one it was aimed straight at but slightly early, or the one on time in
## the lane next door. At 1.5 a whole lane of aiming error -- 60 degrees --
## trades against 90 ms of timing error, so a note in the lane the flick really
## pointed at wins unless it is most of a window away. Raise it and the
## tolerance narrows in practice without the slider moving; drop it to zero and
## a flick takes whatever note is nearest in time within reach, which feels
## like the game guessing.
const AIM_COST_MS_PER_DEG: float = 1.5


## Whether a board playing `hand` is allowed to hit this note.
##
## An untagged input plays everything, which is one board, a mouse, or a key --
## the arrangement this game had before there were two boards and still the
## normal one. A tagged board reaches its own colour and the notes the chart
## marked `any`, and nothing else: the player is holding one board in each hand,
## and a left-hand flick landing on a pink note would be scoring a movement they
## did not make. Bonus notes are open to either, because they are drawn in their
## own gold and belong to neither hand.
func _hand_may_hit(hand: String, n: Dictionary) -> bool:
	if hand == "":
		return true
	if bool(n["bonus"]):
		return true
	var note_hand := String(n["hand"])
	return note_hand == hand or note_hand == "any"


func _try_hit_direction(angle_deg: float, lag_ms: float,
		hand: String = "") -> bool:
	## Score an input that named a direction rather than a lane -- a flick.
	##
	## Separate from _try_hit() rather than folded into it, because a flick is
	## a different kind of measurement from a key press and the difference is
	## not a detail. A key names a lane exactly and instantly; a flick names a
	## bearing measured off a hand-thrown gesture, good to a lane at best, and
	## timed from the peak of a rotation rather than from an edge. Snapping it
	## to the nearest lane and judging it on the keyboard's windows throws that
	## away: a flick 35 degrees wide of a note reads as a miss even though
	## there was nothing else it could have meant, and the player has no way to
	## tell that from having flicked too gently.
	##
	## So the flick reaches every note within `lane_tolerance_deg` of where it
	## went, on windows widened by `timing_scale`, and takes the one that best
	## explains it -- nearest in time, with being aimed wide counted against a
	## note at AIM_COST_MS_PER_DEG. Both limits are in the debug panel, and both
	## can be turned back down to exactly the old behaviour.
	##
	## Exactly one note, always. The loop below picks a single best match and
	## `_accept_hit` is told to settle it outright, so a flick can neither open
	## a hold it has no way to sustain nor be spread across two notes at once.
	## One flick is one note: that is the whole rule, and it is what makes a
	## flick's result something a player can predict.
	var at: float = song_time - lag_ms / 1000.0
	var scale: float = _imu_window_scale()
	var near: float = win_near * scale
	var tolerance: float = clampf(ImuSettings.lane_tolerance_deg, 30.0, 90.0)

	var best: Dictionary = {}
	var best_cost: float = 1e9
	var best_err: float = 0.0

	# Why nothing matched, gathered as we go. A flick that scores nothing is
	# otherwise completely silent, and silence is the one answer a player
	# cannot act on -- it looks identical to a flick the board never saw, to a
	# bridge that is not running, and to a socket that would not bind. The
	# bridge already refuses to be silent about the movements *it* turns down;
	# this is the same courtesy for the ones it accepted and the game did not.
	var waiting: int = 0
	var near_aim: float = 1e9        # smallest angle to any waiting note
	var near_aim_angle: float = 0.0
	var near_aim_err: float = 0.0
	var near_time: float = 1e9       # smallest |timing error| among notes in reach

	for n in notes:
		if String(n["state"]) != "wait":
			continue
		if not _hand_may_hit(hand, n):
			continue
		waiting += 1
		var aim: float = _angle_gap(angle_deg, float(n["angle"]))
		var err: float = (at - float(n["t"])) * 1000.0
		# Tracked over notes that are roughly contemporary, so "you aimed 70
		# degrees wide" is about a note that was actually due rather than about
		# one three minutes away that happens to sit in the right lane.
		if aim < near_aim and absf(err) <= maxf(near, 400.0):
			near_aim = aim
			near_aim_angle = float(n["angle"])
			near_aim_err = err
		if aim > tolerance:
			continue
		if absf(err) < near_time:
			near_time = absf(err)
		if absf(err) > near:
			continue
		var cost: float = absf(err) + aim * AIM_COST_MS_PER_DEG
		if cost < best_cost:
			best = n
			best_cost = cost
			best_err = err
	if best.is_empty():
		_explain_no_hit(angle_deg, waiting, near_aim, near_aim_angle,
			near_aim_err, near_time, tolerance, near, hand)
		return false
	var verdict: String = _judge_for(best_err, scale)
	if verdict == "":
		return false
	_accept_hit(best, verdict, true)
	return true


## Say why a flick that the bridge accepted scored nothing here.
##
## The bridge's own refusals already explain themselves -- it says a movement
## was too gentle, or was mostly a roll, and what to change. This is the other
## half, and until now it did not exist: a flick that passed every test the
## bridge has, arrived, and simply did not land on a note produced no output at
## all. From the player's side that is indistinguishable from the board being
## unplugged, and it is the state somebody is in when they say "it's pointing
## the right way and nothing happens".
##
## Written through the same channel as a bridge refusal, so it appears on screen
## in the place refusals already appear and nothing new has to be drawn. Nothing
## is scored from it, obviously -- it exists to be read.
func _explain_no_hit(angle_deg: float, waiting: int, near_aim: float,
		near_aim_angle: float, near_aim_err: float, near_time: float,
		tolerance: float, near: float, hand: String = "") -> void:
	var colour: String = ""
	if hand != "":
		colour = " blue" if hand == "left" else " pink"
	var text: String
	if waiting == 0:
		text = ("that flick was fine -- there were no%s notes left to hit"
			% colour)
	elif near_aim > 1e8:
		# Notes of that colour are still to come, but none is anywhere near due,
		# so there is nothing to have aimed at and no aim to report. Saying "you
		# aimed a billion degrees wide" is what the branch below did with the
		# sentinel, and it happens twice as often with two boards: each one sees
		# only its own colour, so either can easily flick into a stretch of
		# chart that belongs entirely to the other.
		text = ("nothing%s was due just then -- that flick was early by more "
			+ "than the window, or the notes there are the other colour"
			) % colour
	elif near_aim > tolerance:
		# Aimed wide. The lane it should have gone to is worth naming, because
		# "70 degrees off" is a number and "the lane at 120" is a place.
		text = ("aimed %.0f deg wide -- flick went to %.0f deg, nearest%s note "
			+ "is the lane at %.0f, and the limit is %.0f. If every flick does "
			+ "this by about the same amount, run the direction check."
			) % [near_aim, angle_deg, colour, near_aim_angle, tolerance]
	elif near_time < 1e8:
		var when: String = "early" if near_aim_err < 0.0 else "late"
		text = ("%.0f ms %s -- the direction was right, the window is %.0f ms. "
			+ "If flicks feel late rather than being late, the audio offset "
			+ "keys are the fix.") % [near_time, when, near]
	else:
		text = ("no note was both within %.0f deg and within %.0f ms of that "
			+ "flick") % [tolerance, near]

	var arrow := _arrow(hand)
	arrow["refused"] = 1.0
	arrow["text"] = text
	arrow["hit"] = false
	# Printed as well as drawn. The on-screen line fades, and whoever is
	# debugging this is usually reading the console beside the bridge's own
	# output, where the two halves of the story belong together.
	print("[imu] no note for that flick: ", text)


func _on_tap(event: TapEvent) -> void:
	# Mouse/touch/IMU all funnel through TapInputBus and land here, reusing
	# the exact same _try_hit() scoring path as the A/S/D/J/K/L keys.
	if finished:
		_results_tap(event)
		return
	if autoplay or paused or not started:
		# Nothing can be hit here, so nothing was: said out loud because the
		# arrow reads this to decide whether to draw a flick at all, and a stale
		# `true` would show a flick thrown at a paused song as a scoring hit.
		if event.has_direction():
			_arrow(event.hand)["hit"] = false
		return

	# A flick from the IMU names its direction outright: the board is not
	# anywhere on screen, so there is no position to read an angle from.
	if event.has_direction():
		var hit := _try_hit_direction(event.direction_deg, event.lag_ms,
			event.hand)
		TapInputBus.report_judgement(event.source, hit)
		# Recorded here rather than in _on_imu_flick because only this knows
		# whether a note was there. The ordering is what makes it usable:
		# ImuInput reports to the bus before it emits flick_received, so by the
		# time the arrow is told about the flick, this has already run.
		_arrow(event.hand)["hit"] = hit
		return

	var offset: Vector2 = event.screen_position - centre
	if offset.length() < 12.0:
		return # too close to centre to read a direction
	var angle_deg: float = rad_to_deg(atan2(-offset.y, offset.x))
	if angle_deg < 0.0:
		angle_deg += 360.0
	_try_hit(_nearest_sector(angle_deg))


func _results_tap(event: TapEvent) -> void:
	## Leaving the results screen with nothing but the board in your hand.
	##
	## Without this the board can start a song and play it but not get out of
	## the screen at the end, which leaves "play with the IMU" needing a
	## keyboard anyway -- and needing it at the one moment the player has both
	## hands on the board.
	##
	## Up and down rather than any flick, because the results screen appears
	## the instant the last note resolves and a player mid-gesture would
	## otherwise restart the song by accident. Requiring a deliberate vertical
	## flick also leaves the sideways lanes free to mean nothing here, which is
	## what a stray flick usually is.
	if event.source != "imu" or finish_ui < 0.0:
		return
	match event.vertical():
		1:
			_restart()
		-1:
			get_tree().change_scene_to_file("res://Start.tscn")


func _finish(n: Dictionary, verdict: String, whole: bool = false) -> void:
	n["state"] = "done"
	n["judged"] = verdict
	n["hit_at"] = song_time
	_award(n, verdict, whole)
	_popup(n, verdict, _verdict_colour(verdict))


func _award(n: Dictionary, verdict: String, whole: bool = false) -> void:
	## `whole` pays a hold note out in one award rather than the two it is
	## normally split into. Only a flick passes it, and it has to: a flick
	## settles the note on the spot, so the second award -- the one the hold's
	## release would have earned -- is never coming, and without this the note
	## would silently be worth half of what the score total was computed from.
	counts[verdict] = int(counts.get(verdict, 0)) + 1
	if verdict == "MISS":
		combo = 0
		return
	var awards: float = 2.0 if float(n["hold"]) > 0.0 and not whole else 1.0
	var share: float = (_note_weight(n) / awards) / total_weight
	var factor: float = 1.0 if verdict == "PERFECT" else 0.5
	score_f += share * SCORE_POOL * factor
	score = int(round(score_f))
	combo += 1
	best_combo = maxi(best_combo, combo)
	combo_pop = 1.0


func _verdict_colour(v: String) -> Color:
	match v:
		"PERFECT": return COL_PERFECT
		"EARLY": return COL_EARLY
		"LATE": return COL_LATE
		_: return COL_MISS


func _popup(n: Dictionary, text: String, col: Color) -> void:
	popups.append({
		"text": text, "col": col, "age": 0.0,
		"angle": float(n["angle"]),
	})


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.pressed \
			and event.button_index == MOUSE_BUTTON_LEFT:
		if pause_rect.has_point(event.position):
			_toggle_pause()
		else:
			TapInputBus.report_tap("mouse", event.position)
		return

	if event is InputEventScreenTouch and event.pressed:
		TapInputBus.report_tap("touch", event.position)
		return

	if not (event is InputEventKey and event.pressed):
		return
	var k: int = event.keycode

	if not event.echo:
		match k:
			KEY_ESCAPE:
				get_tree().change_scene_to_file("res://Start.tscn")
				return
			KEY_SPACE:
				_toggle_pause()
				return
			KEY_R:
				_restart()
				return
			KEY_F:
				autoplay = not autoplay
				return
			KEY_N:
				show_numbers = not show_numbers
				return
			KEY_I:
				# Through the settings, so the key and the panel's checkbox
				# are the same switch and it survives the next run.
				ImuSettings.show_arrow = not ImuSettings.show_arrow
				ImuSettings.save_settings()
				ImuSettings.changed.emit()
				return
			KEY_O:
				# Next to I on purpose: that one hides the arrow entirely, this
				# one leaves only the swings that registered as flicks.
				ImuSettings.arrow_flicks_only = not ImuSettings.arrow_flicks_only
				ImuSettings.save_settings()
				ImuSettings.changed.emit()
				return
			KEY_BRACKETLEFT:
				audio_offset_ms -= 5.0
			KEY_BRACKETRIGHT:
				audio_offset_ms += 5.0
			KEY_SEMICOLON:
				audio_offset_ms -= 25.0
			KEY_APOSTROPHE:
				audio_offset_ms += 25.0
			KEY_COMMA:
				_set_speed(speed_mult - 0.05)
			KEY_PERIOD:
				_set_speed(speed_mult + 0.05)

	if event.echo or autoplay or paused or not started:
		return
	if key_sector.has(k):
		_try_hit(int(key_sector[k]))


func _set_speed(v: float) -> void:
	speed_mult = clampf(snappedf(v, 0.05), 0.75, 3.0)
	travel_target = travel_base / speed_mult


func _toggle_pause() -> void:
	paused = not paused
	if player:
		player.stream_paused = paused
	if video:
		video.paused = paused


func _restart() -> void:
	if player:
		player.stop()
	if video:
		video.queue_free()
		video = null
	_setup_video()
	last_seek_at = -999.0
	started = false
	finished = false
	finish_ui = -1.0
	_score_saved = false
	paused = false
	song_time = -start_delay
	score = 0
	score_f = 0.0
	score_shown = 0.0
	combo = 0
	best_combo = 0
	counts = {"PERFECT": 0, "EARLY": 0, "LATE": 0, "MISS": 0}
	popups.clear()
	ring_flash.clear()
	for n in notes:
		n["state"] = "wait"
		n["judged"] = ""
		n["hit_at"] = -999.0
	queue_redraw()


func _vec(angle_deg: float) -> Vector2:
	var a: float = deg_to_rad(angle_deg)
	return Vector2(cos(a), -sin(a))


func _arc_poly(r: float, a_c: float, half_deg: float, thick: float) -> PackedVector2Array:
	# ── FIX: increased segment density (was half_deg * 1.6, min 4, max 40)
	#    so arcs are smoother and don't look faceted / pixelated.
	var steps: int = clampi(int(half_deg * 3.0), 8, 64)
	var outer := PackedVector2Array()
	var inner := PackedVector2Array()
	# Keep both edges on the near side of the centre, and refuse the band
	# outright when it does not survive that.
	#
	# Notes begin their travel at the middle, so a small r is normal. The halo
	# passes ask for a band 2.3x the note's thickness, and the nine-band loop
	# asks for centre radii as low as r - thick/2, which is negative there. A
	# negative radius does not draw a smaller band: `centre + v * -k` lands on
	# the opposite side of the ring, so the edge mirrors, the polygon crosses
	# itself, and triangulation rejects it -- once per band per note per frame.
	# Clamping to zero only trades that for a run of identical points at the
	# centre, which is degenerate too, so the floor is a small positive radius
	# and a band left thinner than a quarter pixel is simply not drawn.
	var r_out: float = r + thick * 0.5
	var r_in: float = maxf(0.5, r - thick * 0.5)
	if r_out < r_in + 0.25:
		return PackedVector2Array()
	for i in steps + 1:
		var a: float = lerpf(a_c - half_deg, a_c + half_deg, float(i) / steps)
		var v := _vec(a)
		outer.append(centre + v * r_out)
		inner.append(centre + v * r_in)
	inner.reverse()
	var poly := outer
	poly.append_array(inner)
	return poly


func _fill_arc(r: float, a_c: float, half_deg: float, thick: float,
			   col: Color) -> void:
	## Draw an arc band, skipping the ones _arc_poly refused as undrawable.
	var poly := _arc_poly(r, a_c, half_deg, thick)
	if poly.size() >= 3:
		draw_colored_polygon(poly, col)


func _arc_line(r: float, a_c: float, half_deg: float) -> PackedVector2Array:
	# ── FIX: increased segment density (was half_deg * 1.6, min 4, max 40)
	#    so arc outlines are smoother.
	var steps: int = clampi(int(half_deg * 3.0), 8, 64)
	var pts := PackedVector2Array()
	for i in steps + 1:
		var a: float = lerpf(a_c - half_deg, a_c + half_deg, float(i) / steps)
		pts.append(centre + _vec(a) * r)
	return pts


func _rims(n: Dictionary) -> Array:
	if bool(n["bonus"]):
		return [G_OUTER, G_INNER]
	if String(n["hand"]) == "left":
		return [L_OUTER, L_INNER]
	return [R_OUTER, R_INNER]


func _band_colour(outer_c: Color, inner_c: Color, t: float) -> Color:
	var base: Color = inner_c.lerp(outer_c, t)
	# Wider, gentler bright centre that extends across most of the note,
	# fading to the rim colours only at the very edges.
	var whiten: float = 1.0 - absf(t - 0.5) * 2.0
	whiten = pow(clampf(whiten, 0.0, 1.0), 0.45) * 0.62
	return base.lerp(Color(1, 1, 1), whiten)


func _draw_note_body(r: float, a_c: float, half_len: float, thick: float,
					 outer_c: Color, inner_c: Color, alpha: float,
					 slide_dir: float = 0.0, skip_depth: bool = false,
					 glow_boost: float = 0.0, skip_halo: bool = false) -> void:
	if r < 1.0 or alpha <= 0.01:
		return
	var half_deg: float = minf(rad_to_deg(half_len / maxf(r, 8.0)), 26.0)
	var r_out: float = r + thick * 0.5
	var r_in: float = maxf(0.5, r - thick * 0.5)

	# ── Soft 3D depth shadow ─────────────────────────────────────────────
	if not skip_depth:
		var depth_px: float = 7.0 * clampf(r / radius, 0.0, 1.0)
		if depth_px > 0.5:
			var depth_offset: Vector2 = _vec(a_c) * depth_px
			var shadow_base: Color = outer_c.lerp(inner_c, 0.5).darkened(0.30)
			var orig_centre: Vector2 = centre
			for di in 5:
				var t_d: float = float(di + 1) / 5.0
				centre = orig_centre + depth_offset * t_d
				var sha: float = 0.14 * (1.0 - t_d) * alpha
				_fill_arc(r, a_c, half_deg, thick,
					Color(shadow_base.r, shadow_base.g, shadow_base.b, sha))
			centre = orig_centre

	# ── Halos (soft glow) ────────────────────────────────────────────────
	if not skip_halo:
		_fill_arc(r, a_c, half_deg * 1.05, thick * 2.0,
			Color(outer_c.r, outer_c.g, outer_c.b, 0.10 * alpha))
		_fill_arc(r, a_c, half_deg * 1.02, thick * 1.3,
			Color(inner_c.r, inner_c.g, inner_c.b, 0.12 * alpha))

	# ── Colour bands (smooth gradient — 24 bands eliminates banding) ─────
	var bands: int = 24
	for i in bands:
		var tc: float = (float(i) + 0.5) / bands
		var c: Color = _band_colour(outer_c, inner_c, tc)
		_fill_arc(r + (tc - 0.5) * thick, a_c, half_deg, thick / bands + 0.9,
			Color(c.r, c.g, c.b, 0.97 * alpha))

	# ── Extra interior glow for bonus / gold notes ───────────────────────
	if glow_boost > 0.01:
		_fill_arc(r, a_c, half_deg * 1.1, thick * 1.6,
			Color(outer_c.r, outer_c.g, outer_c.b, 0.24 * glow_boost * alpha))
		_fill_arc(r, a_c, half_deg * 0.92, thick * 0.85,
			Color(1, 1, 1, 0.30 * glow_boost * alpha))
		_fill_arc(r, a_c, half_deg * 0.70, thick * 0.45,
			Color(1, 1, 1, 0.34 * glow_boost * alpha))

	# ── Outlines (tight / thin) ──────────────────────────────────────────
	var ow: float = 1.2
	var mid_c: Color = outer_c.lerp(inner_c, 0.5)
	draw_polyline(_arc_line(r_out, a_c, half_deg),
		Color(outer_c.r, outer_c.g, outer_c.b, 0.85 * alpha), ow)
	draw_polyline(_arc_line(r_in, a_c, half_deg),
		Color(inner_c.r, inner_c.g, inner_c.b, 0.85 * alpha), ow)
	for side in [-1.0, 1.0]:
		var ea: float = a_c + side * half_deg
		var ev := _vec(ea)
		draw_line(centre + ev * r_in, centre + ev * r_out,
			Color(mid_c.r, mid_c.g, mid_c.b, 0.85 * alpha), ow)

	# ── Slide-direction chevrons (span full note height) ──────────────
	if absf(slide_dir) > 0.01:
		var tang := (_vec(a_c + 1.0) - _vec(a_c - 1.0)).normalized() * signf(slide_dir)
		var rad := _vec(a_c)
		var pos := centre + rad * r
		var sl: float = thick * 0.48
		for k in 2:
			var off := tang * (float(k) * sl * 0.85 - sl * 0.42)
			draw_colored_polygon(PackedVector2Array([
				pos + off + tang * sl * 0.55,
				pos + off - tang * sl * 0.18 + rad * sl * 0.95,
				pos + off - tang * sl * 0.18 - rad * sl * 0.95,
			]), Color(1, 1, 1, 0.80 * alpha))


func _note_thick(r: float, scale: float = 1.0) -> float:
	var f: float = clampf(r / radius, 0.0, 1.0)
	return lerpf(bar_thickness * 0.40, bar_thickness, f) * scale


func _radius_at(lead: float) -> float:
	return radius * clampf(1.0 - lead / travel, 0.0, 1.0)


func _ribbon_poly(ang: PackedFloat32Array, rad: PackedFloat32Array,
				  wid: PackedFloat32Array, mul: float) -> PackedVector2Array:
	var outer := PackedVector2Array()
	var inner := PackedVector2Array()
	# Notes travel outward from the centre, so a slide's tail sits at a small
	# radius -- and once that radius drops below the ribbon's own half-width
	# there is no ribbon to draw there. Left alone, rad - hw goes negative and
	# the inner edge does not shrink to nothing, it mirrors to the far side of
	# the ring; the polygon crosses itself, and triangulation refuses a
	# self-intersecting polygon once per ribbon per frame. Clamping the inner
	# radius to zero instead of dropping the points only trades that for a run
	# of identical points at the centre, which is degenerate in its own right.
	#
	# Radius falls monotonically along the ribbon, so the undrawable part is
	# always a tail: stopping at it truncates rather than punching a hole.
	for i in ang.size():
		var hw: float = wid[i] * mul * 0.5
		if rad[i] - hw <= 0.5:
			break
		var v := _vec(ang[i])
		outer.append(centre + v * (rad[i] + hw))
		inner.append(centre + v * (rad[i] - hw))
	if outer.size() < 2:
		return PackedVector2Array()
	inner.reverse()
	var poly := outer
	poly.append_array(inner)
	return poly


func _draw_slide(n: Dictionary) -> void:
	var rim: Array = _rims(n)
	var outer_c: Color = rim[0]
	var inner_c: Color = rim[1]
	var mid_c: Color = outer_c.lerp(inner_c, 0.5)
	var hold: float = float(n["hold"])
	var span: float = hold_dps * hold * float(n["sweep"])
	var a0: float = float(n["angle"])
	var lit: bool = String(n["state"]) == "holding"

	# ── Ribbon geometry ──────────────────────────────────────────────────
	# Ribbon uses the SAME width as the head/tail caps so the whole slide
	# reads as one continuous shape rather than a wide band with two
	# narrower bars stuck on the ends.
	var ribbon_scale: float = slide_width_scale
	var steps: int = 64
	var ang := PackedFloat32Array()
	var rad := PackedFloat32Array()
	var wid := PackedFloat32Array()
	var uvals := PackedFloat32Array()
	for i in steps + 1:
		var u: float = float(i) / steps
		var lead_u: float = (float(n["t"]) + u * hold) - song_time
		if lead_u > travel:
			break
		var ru: float = _radius_at(lead_u)
		ang.append(a0 + span * u)
		rad.append(ru)
		wid.append(_note_thick(ru, ribbon_scale))
		uvals.append(u)
	if ang.size() < 2:
		return

	# Current hit position (u = 0 at head, 1 at tail)
	var hit_u: float = 0.0
	if lit:
		var elapsed: float = clampf(song_time - float(n["t"]), 0.0, hold)
		hit_u = elapsed / hold

	# ── Single soft halo (multiple layers created visible ghost arcs) ────
	var halo_poly := _ribbon_poly(ang, rad, wid, 1.35)
	if halo_poly.size() >= 3:
		draw_colored_polygon(halo_poly,
			Color(mid_c.r, mid_c.g, mid_c.b, 0.10))

	# ── Main filled ribbon ───────────────────────────────────────────────
	var core := _band_colour(outer_c, inner_c, 0.5)
	var core_poly := _ribbon_poly(ang, rad, wid, 1.0)
	if core_poly.size() >= 3:
		draw_colored_polygon(core_poly,
			Color(core.r, core.g, core.b, 0.80))

	# Brighter inner highlight band
	var inner_band := _ribbon_poly(ang, rad, wid, 0.45)
	if inner_band.size() >= 3:
		var bright: Color = core.lerp(Color(1, 1, 1), 0.30)
		draw_colored_polygon(inner_band,
			Color(bright.r, bright.g, bright.b, 0.50))

	# ── Fade the passed portion when holding ─────────────────────────────
	if lit and hit_u > 0.01:
		var passed_ang := PackedFloat32Array()
		var passed_rad := PackedFloat32Array()
		var passed_wid := PackedFloat32Array()
		for i in uvals.size():
			if uvals[i] > hit_u:
				break
			passed_ang.append(ang[i])
			passed_rad.append(rad[i])
			passed_wid.append(wid[i])
		if passed_ang.size() >= 2:
			var fade_poly := _ribbon_poly(passed_ang, passed_rad, passed_wid, 1.05)
			if fade_poly.size() >= 3:
				draw_colored_polygon(fade_poly, Color(0.01, 0.01, 0.03, 0.50))

	# ── Glow at the current hit position ─────────────────────────────────
	if lit:
		var hit_angle: float = a0 + span * hit_u
		var hit_lead: float = (float(n["t"]) + hit_u * hold) - song_time
		var hit_r: float = _radius_at(hit_lead)
		var hit_w: float = _note_thick(hit_r, ribbon_scale)
		var glow_half: float = minf(rad_to_deg(hit_w / maxf(hit_r, 8.0)), 18.0)
		_fill_arc(hit_r, hit_angle, glow_half * 1.6, hit_w * 2.2,
			Color(mid_c.r, mid_c.g, mid_c.b, 0.18))
		_fill_arc(hit_r, hit_angle, glow_half, hit_w * 1.4,
			Color(1, 1, 1, 0.10))

	# ── Edge outlines (thin) ─────────────────────────────────────────────
	var out_pts := PackedVector2Array()
	var in_pts := PackedVector2Array()
	for i in ang.size():
		var v := _vec(ang[i])
		out_pts.append(centre + v * (rad[i] + wid[i] * 0.5))
		in_pts.append(centre + v * (rad[i] - wid[i] * 0.5))
	draw_polyline(out_pts, Color(outer_c.r, outer_c.g, outer_c.b, 0.65), 1.4)
	draw_polyline(in_pts, Color(inner_c.r, inner_c.g, inner_c.b, 0.65), 1.4)

	# ── Fluid head / tail caps ───────────────────────────────────────────
	# Drawn with skip_depth AND skip_halo so they add no glow or shadow of
	# their own -- they are just the brighter colour bands sitting exactly on
	# the ribbon, which is already the same width, so the slide reads as one
	# continuous shape with brighter ends.
	var tail_lead: float = (float(n["t"]) + hold) - song_time
	if tail_lead <= travel:
		var r_end: float = _radius_at(tail_lead)
		var len_end: float = lerpf(bar_len_centre, bar_len_edge, r_end / radius) * 0.5
		_draw_note_body(r_end, a0 + span, len_end * 0.82,
			_note_thick(r_end, ribbon_scale), outer_c, inner_c, 0.9,
			0.0, true, 0.0, true)

	var head_lead: float = float(n["t"]) - song_time
	var r_head: float = _radius_at(head_lead)
	var hu: float = clampf(-head_lead / hold, 0.0, 1.0) if hold > 0.0 else 0.0
	var len_head: float = lerpf(bar_len_centre, bar_len_edge, r_head / radius) * 0.5
	_draw_note_body(r_head, a0 + span * hu, len_head,
		_note_thick(r_head, ribbon_scale), outer_c, inner_c, 1.0,
		float(n["sweep"]), true, 0.0, true)


func _draw() -> void:
	var vp := get_viewport_rect().size

	# Clipped gradient-text items persist between frames, so wipe them first;
	# _draw_vgrad_text refills whichever it needs this frame.
	_vgrad_clear_all()

	draw_rect(Rect2(Vector2.ZERO, vp), Color(0.01, 0.01, 0.03, scrim_alpha))

	var res_t: float = -1.0
	if finished and finish_ui >= 0.0:
		res_t = ui_time - finish_ui
	var wipe: float = 0.0 if res_t < 0.0 else smoothstep(0.0, 0.5, res_t)

	if wipe < 1.0:
		_draw_playfield()
		# Under the notes on purpose: it lives in the middle of the ring, which
		# is where every note is born, and a note that a player is about to hit
		# must never be the thing that gets covered up.
		_draw_imu_arrow()
		if not finished:
			_draw_notes()
		_draw_popups()
		_draw_hud(vp)
		_draw_progress(vp)
	if wipe > 0.0:
		draw_rect(Rect2(Vector2.ZERO, vp), Color(0.02, 0.02, 0.05, wipe))

	if not finished:
		_draw_intro(vp)
	if finished:
		_draw_results(vp)


func _dotted_circle(r: float, col: Color, dash_deg: float, gap_deg: float,
					width: float) -> void:
	var a: float = 0.0
	while a < 360.0:
		var b: float = minf(a + dash_deg, 360.0)
		draw_arc(centre, r, deg_to_rad(a), deg_to_rad(b), 6, col, width)
		a = b + gap_deg


func _draw_playfield() -> void:
	for f in [0.25, 0.50, 0.75]:
		_dotted_circle(radius * f, Color(0.72, 0.66, 1.0, 0.20), 3.2, 4.4, 1.6)

	draw_arc(centre, radius, 0, TAU, 160, Color(0.80, 0.76, 1.00, 0.16), 9.0)
	draw_arc(centre, radius, 0, TAU, 160, Color(1, 1, 1, 0.80), 2.6)

	for sect in sector_angle:
		var ang: float = float(sector_angle[sect])
		var v := _vec(ang)
		var right_side: bool = int(sect) <= 3
		var tint: Color = R_OUTER if right_side else L_OUTER

		for i in 6:
			var t0: float = float(i) / 6.0
			var t1: float = float(i + 1) / 6.0
			draw_line(centre + v * radius * t0, centre + v * radius * t1,
				Color(tint.r, tint.g, tint.b, 0.05 + 0.30 * t1), 1.4)

		var fl: float = float(ring_flash.get(sect, 0.0))
		var pad: float = 27.0
		draw_polyline(_arc_line(radius, ang, pad),
			Color(tint.r, tint.g, tint.b, 0.26 + 0.70 * fl), 2.4 + 6.0 * fl)
		if fl > 0.01:
			draw_polyline(_arc_line(radius, ang, pad),
				Color(1, 1, 1, 0.5 * fl), 1.6)
		draw_circle(centre + v * radius, 5.0 + 5.0 * fl,
			Color(1, 1, 1, 0.30 + 0.6 * fl))

		if show_numbers:
			var lp := centre + v * (radius + 34.0)
			draw_string(font_bold, lp - Vector2(6, -7), str(sect), 0, -1, 20,
				Color(tint.r, tint.g, tint.b, 0.85))


## How far apart the two arrows are planted, as a fraction of the ring's
## radius. Far enough that two boards held still are two dots rather than one
## smudge, and near enough that each arrow still reads as coming from the
## middle and pointing at a lane.
const IMU_ARROW_SPLIT: float = 0.085


func _draw_imu_arrow() -> void:
	## An arrow per board in the middle of the ring, showing what is in the hand.
	##
	## It points where the board is being swung, grows with how hard, and
	## flashes down the lane a flick landed in. Drawn in the same angle
	## convention and from the same centre as the lanes themselves, so "the
	## arrow points at lane 3" and "the flick hit lane 3" are the same
	## statement -- which is what makes it a check on the mapping and not just
	## decoration.
	##
	## Only shown while the bridge is actually feeding the game. A permanent
	## arrow stuck at rest would tell a mouse player their board was connected.
	if not show_imu_arrow or not ImuInput.enabled or not ImuInput.link_up:
		return
	if not ImuInput.board_connected:
		return          # the bridge is talking, but not about a board

	var shown: Array = ImuInput.active_hands()
	for index in shown.size():
		var hand := String(shown[index])
		if not ImuInput.hand_connected(hand):
			continue    # that board has gone; the other one keeps its arrow
		# Planted left and right of centre so two arrows can be told apart at a
		# glance even when both boards are doing the same thing -- and on the
		# side matching the hand that holds them, so which arrow is which is
		# something you read off the screen rather than remember.
		var origin := centre
		if shown.size() > 1:
			origin.x += (-1.0 if hand == "left" else 1.0) \
				* radius * IMU_ARROW_SPLIT
		_draw_one_imu_arrow(hand, _arrow(hand), origin, shown.size() > 1)


func _draw_one_imu_arrow(hand: String, arrow: Dictionary, origin: Vector2,
		two_boards: bool) -> void:
	var imu_angle: float = float(arrow["angle"])
	var imu_reach: float = float(arrow["reach"])
	var imu_flash: float = float(arrow["flash"])
	var imu_refused: float = float(arrow["refused"])
	var imu_last_hit: bool = bool(arrow["hit"])
	if is_nan(imu_angle):
		return

	# Flicks only: draw movements the detector accepted, and nothing else.
	#
	# A different question from the colour rule below. That one is about
	# scoring -- did that flick land on a note. This one is about detection:
	# was that swing strong enough and clean enough to be sent as an input at
	# all. So it keys off the flick arriving, not off whether there was a note
	# where it went, and what it removes is everything that is not a flick --
	# the live arrow tracking every wobble of an unsteady board, the rest dot,
	# and the mark for a movement that was refused.
	var flicks_only: bool = ImuSettings.arrow_flicks_only
	if flicks_only and imu_flash <= 0.01:
		return

	# Squared, so a flick reads as a strike that fades rather than a light
	# being switched off: most of the brightness goes in the first fifth of a
	# second and the tail is long enough to see where it pointed.
	var pulse: float = imu_flash * imu_flash
	var refused: float = imu_refused * imu_refused
	var reach: float = maxf(imu_reach, maxf(pulse, refused * 0.8))
	if flicks_only:
		# The flick's own fade, and nothing of how hard the board happens to
		# still be swinging afterwards. imu_flash rather than its square keeps
		# the arrow above the vanishing threshold for as long as it is drawn.
		reach = maxf(pulse, imu_flash)
	if refused > 0.01 and not flicks_only:
		_draw_imu_refusal(refused, String(arrow["text"]), origin, hand,
			two_boards)
	if reach < 0.01:
		# Still board, no flick: a dot, so the arrow has somewhere to grow from
		# and its absence still means "no board" rather than "not moving".
		draw_circle(origin, 3.0, Color(1, 1, 1, 0.22))
		return

	var v := _vec(imu_angle)
	var side := Vector2(-v.y, v.x)

	# The lane's own colour, so the arrow says which lane it would hit without
	# needing a number: right hand lanes pink, left hand lanes blue, exactly as
	# the notes arriving in them are coloured.
	#
	# With two boards the arrow takes its board's colour instead, and keeps it
	# wherever the board is pointed. The lane colour would be saying something
	# the player already knows -- a blue board can only hit blue notes -- while
	# hiding the one thing two arrows have to say, which of them is moving.
	#
	# Except that colour is reserved for a flick that actually scored. Live
	# movement is not a hit, a refusal is not a hit, and a well-aimed flick
	# into an empty lane is not a hit either -- all three draw grey. It makes
	# the screen answer "did that count" at a glance, which is the question
	# being asked over and over while a board is being set up.
	var lane: int = _nearest_sector(imu_angle)
	var tint: Color = R_OUTER if lane <= 3 else L_OUTER
	if two_boards:
		tint = L_OUTER if hand == "left" else R_OUTER
	if ImuSettings.colour_only_hits and not (imu_last_hit and imu_flash > 0.0):
		tint = IMU_GREY
	var col: Color = tint.lerp(Color(1, 1, 1), 0.35 + 0.45 * pulse)
	# The floor is high enough to stay readable over the brightest frames of
	# the video behind it. A half-swing that fades into the background would
	# leave the arrow only visible at the two extremes -- rest and flick -- and
	# the whole point of it is the range in between.
	var alpha: float = minf(0.95, 0.30 + 0.45 * reach + 0.25 * pulse)

	var r0: float = radius * 0.09
	var r1: float = radius * (0.17 + 0.40 * reach)
	var head: float = 13.0 + 11.0 * reach
	var half_w: float = 7.0 + 5.5 * reach
	var tip := origin + v * r1
	var neck := origin + v * maxf(r0, r1 - head)

	# A wide, faint pass under a narrow bright one: the glow keeps the arrow
	# readable over the video without the shaft itself having to be thick
	# enough to hide notes behind it.
	draw_line(origin + v * r0, neck,
		Color(tint.r, tint.g, tint.b, alpha * 0.34), 10.0 + 9.0 * reach)
	draw_line(origin + v * r0, neck,
		Color(col.r, col.g, col.b, alpha), 2.6 + 2.6 * reach)
	draw_colored_polygon(PackedVector2Array([
		tip, neck + side * half_w, neck - side * half_w]),
		Color(col.r, col.g, col.b, alpha))
	draw_circle(origin, 3.0 + 3.0 * reach,
		Color(col.r, col.g, col.b, minf(0.9, alpha + 0.15)))

	# At full reach the swing is past the threshold, so a flick in this
	# direction would be accepted. Marking the lane it would go to is what
	# turns "flick harder" from guesswork into something you can aim.
	if imu_reach > 0.98:
		draw_polyline(_arc_line(radius, float(sector_angle[lane]), 13.0),
			Color(1, 1, 1, 0.30), 2.0)

	if pulse > 0.01:
		# The flick itself, thrown down its lane to the rim. White for a hit,
		# grey for one that scored nothing -- same rule as the shaft.
		var streak := Color(1, 1, 1)
		if ImuSettings.colour_only_hits and not imu_last_hit:
			streak = IMU_GREY
		var strength: float = clampf(
			float(ImuInput.state_of(hand)["strength"]), 0.0, 1.0)
		var flick_end: float = radius * (0.62 + 0.30 * strength)
		draw_line(tip, origin + v * flick_end,
			Color(streak.r, streak.g, streak.b, 0.55 * pulse), 2.0 + 4.0 * pulse)
		draw_circle(origin + v * flick_end, 4.0 + 9.0 * pulse,
			Color(streak.r, streak.g, streak.b, 0.45 * pulse))


func _draw_imu_refusal(refused: float, text: String, origin: Vector2,
		hand: String, two_boards: bool) -> void:
	## A dashed ring and the bridge's own sentence, for a movement that was
	## seen and not counted. Grey rather than a lane colour, because the point
	## is precisely that no lane was chosen.
	var col := Color(0.85, 0.80, 0.90, 0.30 + 0.45 * refused)
	_dotted_circle(radius * 0.30, col, 6.0, 7.0, 2.0)
	if text == "":
		return
	# Named when there are two, because "flick harder" is advice about one hand
	# and following it with the wrong one is worse than not following it. The
	# two sentences also sit one above the other rather than on top of each
	# other, since both boards can be refused within the same second.
	var line: String = text
	var drop: float = 0.0
	if two_boards:
		line = "%s: %s" % [ImuInput.hand_label(hand), text]
		drop = 0.0 if hand == "left" else 20.0
	var size: int = 14
	var w: float = font_bold.get_string_size(line, 0, -1, size).x
	var at := Vector2(origin.x - w * 0.5,
		centre.y + radius * 0.30 + 26.0 + drop)
	draw_string_outline(font_bold, at, line, 0, -1, size, 5,
		Color(0.02, 0.01, 0.06, 0.75 * refused))
	draw_string(font_bold, at, line, 0, -1, size,
		Color(1.0, 0.86, 0.86, 0.35 + 0.55 * refused))


func _draw_notes() -> void:
	for pid in pairs:
		var pr: Array = pairs[pid]
		if pr.size() < 2 or String(pr[0]["state"]) != "wait":
			continue
		var lead: float = float(pr[0]["t"]) - song_time
		if lead > travel or lead < 0.0:
			continue
		var p: float = 1.0 - lead / travel
		var a0: float = float(pr[0]["angle"])
		var a1: float = float(pr[1]["angle"])
		if absf(a1 - a0) > 180.0:
			a1 += 360.0 if a1 < a0 else -360.0

		# ── FIX: filled band spanning full note vertices instead of thin line
		var band_r: float = radius * p
		var band_thick: float = _note_thick(band_r) * 0.85
		# Extend arc to include the half-width of the notes at each end
		var hl: float = lerpf(bar_len_centre, bar_len_edge, p) * 0.5
		var note_half_deg: float = minf(rad_to_deg(hl / maxf(band_r, 8.0)), 26.0)
		var a_min: float = minf(a0, a1) - note_half_deg
		var a_max: float = maxf(a0, a1) + note_half_deg
		var mid_a: float = (a_min + a_max) * 0.5
		var span_half: float = (a_max - a_min) * 0.5

		_fill_arc(band_r, mid_a, span_half, band_thick,
			Color(1, 1, 1, 0.14 * p))
		# Single line down the centre of the band
		draw_polyline(_arc_line(band_r, mid_a, span_half),
			Color(1, 1, 1, 0.70 * p), 1.6)

	for n in notes:
		var hold: float = float(n["hold"])
		var lead: float = float(n["t"]) - song_time
		var st: String = String(n["state"])

		if st == "done":
			var since: float = song_time - float(n["hit_at"])
			if since >= 0.0 and since < 0.20 and String(n["judged"]) != "MISS":
				var rim0: Array = _rims(n)
				var a: float = 1.0 - since / 0.20
				_draw_note_body(radius, float(n["angle"]),
					bar_len_edge * 0.5 * (1.0 + 0.55 * (1.0 - a)),
					bar_thickness * (1.0 + 0.7 * (1.0 - a)),
					rim0[0], rim0[1], a * 0.75)
			continue

		if lead > travel or lead < -(hold + 0.3):
			continue

		if hold > 0.0:
			_draw_slide(n)
		else:
			var rim: Array = _rims(n)
			var p: float = clampf(1.0 - lead / travel, 0.0, 1.0)
			var hl: float = lerpf(bar_len_centre, bar_len_edge, p) * 0.5
			var alpha: float = 1.0
			if lead < 0.0:
				alpha = clampf(1.0 + lead / (win_near / 1000.0), 0.25, 1.0)
			alpha *= smoothstep(0.0, 0.10, p)
			var rr: float = radius * p
			var glow: float = 0.6 if bool(n["bonus"]) else 0.0
			_draw_note_body(rr, float(n["angle"]), hl, _note_thick(rr),
				rim[0], rim[1], alpha, 0.0, false, glow)


func _draw_popups() -> void:
	for p in popups:
		var age: float = float(p["age"])
		var t: float = age / 0.7
		var alpha: float = 1.0 - pow(t, 2.4)
		var out: float = 26.0 + 16.0 * t
		var ang: float = float(p["angle"])
		var txt: String = String(p["text"])
		var col: Color = p["col"]
		var size: int = 19
		var w: float = font_heavy.get_string_size(txt, 0, -1, size).x
		var dirv := _vec(ang)
		var clear: float = absf(dirv.x) * w * 0.5 + absf(dirv.y) * size * 0.6
		var anchor := centre + dirv * (radius + out + clear)
		var pos := anchor - Vector2(w * 0.5, -6.0)
		draw_string_outline(font_heavy, pos, txt, 0, -1, size, 5,
			Color(0.02, 0.01, 0.05, 0.85 * alpha))
		draw_string(font_heavy, pos, txt, 0, -1, size,
			Color(col.r, col.g, col.b, alpha))


func _draw_intro(vp: Vector2) -> void:
	var tail: float = 1.1
	var span: float = start_delay + tail
	var x: float = clampf((song_time + start_delay) / span, 0.0, 1.0)
	if x >= 1.0:
		return
	var k: float = 1.0 - smoothstep(0.0, 1.0, x)
	draw_rect(Rect2(Vector2.ZERO, vp), Color(0, 0, 0, 0.94 * k))

	if song_time >= 0.0:
		return
	var remain: float = -song_time
	var n: int = clampi(int(ceil(remain)), 1, 3)
	var frac: float = remain - floor(remain)
	var pop: float = 1.0 + 0.28 * frac
	var size: int = int(96 * pop)
	var txt: String = str(n)
	var w: float = font_heavy.get_string_size(txt, 0, -1, size).x
	var pos := centre + Vector2(-w * 0.5, size * 0.34)
	var a: float = clampf(1.0 - frac * 0.85, 0.15, 1.0)
	draw_string_outline(font_heavy, pos, txt, 0, -1, size, 12,
		Color(0.35, 0.25, 0.6, 0.5 * a))
	draw_string(font_heavy, pos, txt, 0, -1, size, Color(1, 1, 1, a))


func _draw_hud(vp: Vector2) -> void:
	pause_rect = Rect2(Vector2(22, 20), Vector2(30, 30))
	var pc := Color(1, 1, 1, 0.92)
	if paused:
		draw_colored_polygon(PackedVector2Array([
			pause_rect.position + Vector2(9, 6),
			pause_rect.position + Vector2(9, 24),
			pause_rect.position + Vector2(24, 15)]), pc)
	else:
		draw_rect(Rect2(pause_rect.position + Vector2(8, 6), Vector2(5, 18)), pc)
		draw_rect(Rect2(pause_rect.position + Vector2(18, 6), Vector2(5, 18)), pc)
	draw_string(font_bold, Vector2(64, 43), song_title, 0, -1, 24, Color(1, 1, 1, 0.96))

	var s_txt: String = _fmt_score(int(round(score_shown)))
	var sw: float = font_bold.get_string_size(s_txt, 0, -1, 34).x
	draw_string(font_bold, Vector2(vp.x - 28 - sw, 46), s_txt, 0, -1, 34,
		Color(1, 1, 1, 0.96))

	if combo > 0:
		var lvl: float = clampf(float(combo) / 60.0, 0.0, 1.0)
		var ccol: Color = COMBO_LOW.lerp(COMBO_HIGH, lvl)
		var csize: int = int(38 + 26.0 * lvl + 16.0 * combo_pop)
		var c_txt: String = str(combo)
		var cw: float = font_heavy.get_string_size(c_txt, 0, -1, csize).x
		var cpos := Vector2(vp.x - 28 - cw, 46 + 52 + csize * 0.35)
		if lvl > 0.2:
			draw_string_outline(font_heavy, cpos, c_txt, 0, -1, csize, int(10 * lvl),
				Color(ccol.r, ccol.g, ccol.b, 0.30 * lvl))
		draw_string(font_heavy, cpos, c_txt, 0, -1, csize, ccol)
		var lw: float = font_bold.get_string_size("COMBO", 0, -1, 15).x
		draw_string(font_bold, Vector2(vp.x - 28 - lw, cpos.y + 20), "COMBO", 0, -1, 15,
			Color(ccol.r, ccol.g, ccol.b, 0.75))

	var hint := "A S D / J K L    SPACE pause   R restart   F autoplay   I imu arrow   ESC title   , . speed %.1fx   [ ] ; ' latency %+.0fms" % [speed_mult, audio_offset_ms]
	draw_string(font_bold, Vector2(24, vp.y - 16), hint, 0, -1, 14, Color(1, 1, 1, 0.40))

	if autoplay:
		_draw_gradient_text("AUTOPLAY", Vector2(vp.x * 0.5, 34), 20,
			Color(0.36, 0.78, 1.00), Color(0.72, 0.50, 1.00), 0.92)
	if paused:
		var pw: float = font_heavy.get_string_size("PAUSED", 0, -1, 44).x
		draw_string_outline(font_heavy, Vector2(centre.x - pw * 0.5, centre.y + 8),
			"PAUSED", 0, -1, 44, 8, Color(0, 0, 0, 0.7))
		draw_string(font_heavy, Vector2(centre.x - pw * 0.5, centre.y + 8), "PAUSED",
			0, -1, 44, Color(1, 1, 1, 0.92))
	if load_error != "":
		draw_string(font_bold, Vector2(24, vp.y - 40), load_error, 0, -1, 15,
			Color(1, 0.45, 0.45))


func _draw_gradient_text(txt: String, mid_top: Vector2, size: int,
						 c0: Color, c1: Color, alpha: float) -> void:
	var total: float = font_heavy.get_string_size(txt, 0, -1, size).x
	var x: float = mid_top.x - total * 0.5
	for i in txt.length():
		var ch: String = txt[i]
		var t: float = float(i) / maxf(1.0, float(txt.length() - 1))
		var c: Color = c0.lerp(c1, t)
		var p := Vector2(x, mid_top.y)
		draw_string_outline(font_heavy, p, ch, 0, -1, size, 5,
			Color(0.02, 0.01, 0.06, 0.75 * alpha))
		draw_string(font_heavy, p, ch, 0, -1, size, Color(c.r, c.g, c.b, alpha))
		x += font_heavy.get_string_size(ch, 0, -1, size).x


func _draw_progress(vp: Vector2) -> void:
	var y: float = vp.y - 46.0
	var x0: float = 40.0
	var x1: float = vp.x - 40.0
	var w: float = x1 - x0

	draw_line(Vector2(x0, y), Vector2(x1, y), Color(0.04, 0.03, 0.08, 0.9), 4.0)
	var cap := Vector2(7, 7)
	draw_rect(Rect2(Vector2(x0 - cap.x * 0.5, y - cap.y * 0.5), cap),
		Color(0.07, 0.05, 0.12, 0.95))
	draw_rect(Rect2(Vector2(x1 - cap.x * 0.5, y - cap.y * 0.5), cap),
		Color(0.07, 0.05, 0.12, 0.95))

	var prog: float = _progress()
	var seg: int = 64
	for i in seg:
		var f0: float = float(i) / seg
		if f0 > prog:
			break
		var f1: float = minf(float(i + 1) / seg, prog)
		var mixv: float = 0.5 + 0.5 * sin(f0 * 9.0 + song_time * 0.7)
		var c: Color = BAR_PURPLE.lerp(BAR_BLUE, mixv)
		draw_line(Vector2(x0 + w * f0, y), Vector2(x0 + w * f1, y),
			Color(c.r, c.g, c.b, 0.96), 5.0)

	for sp in sparks:
		if float(sp["x"]) > prog:
			continue
		var tw: float = 0.35 + 0.65 * absf(sin(float(sp["phase"])))
		var c2: Color = BAR_PURPLE if bool(sp["purple"]) else BAR_BLUE
		var pp := Vector2(x0 + w * float(sp["x"]), y + float(sp["y"]) * 5.0)
		draw_circle(pp, float(sp["size"]) * tw, Color(c2.r, c2.g, c2.b, 0.85 * tw))
		draw_circle(pp, float(sp["size"]) * tw * 0.45, Color(1, 1, 1, 0.9 * tw))

	if prog > 0.0 and prog < 1.0:
		var xh: float = x0 + w * prog
		draw_circle(Vector2(xh, y), 7.0, Color(0.85, 0.80, 1.0, 0.30))
		draw_circle(Vector2(xh, y), 3.4, Color(1, 1, 1, 0.95))


const RES_BAND_DARK  := Color(0.086, 0.075, 0.130)
const RES_BAND_MID   := Color(0.150, 0.130, 0.215)
const RES_BAND_LIGHT := Color(0.235, 0.205, 0.320)
const RES_INK        := Color(0.960, 0.955, 0.985)
const RES_INK_DIM    := Color(0.640, 0.620, 0.720)


func _skew_band(cx: float, half_w: float, y: float, h: float, skew: float,
				col: Color) -> void:
	draw_colored_polygon(PackedVector2Array([
		Vector2(cx - half_w + skew, y),
		Vector2(cx + half_w + skew, y),
		Vector2(cx + half_w - skew, y + h),
		Vector2(cx - half_w - skew, y + h),
	]), col)


func _skew_edge(cx: float, half_w: float, y: float, skew: float, col: Color,
				w: float = 1.0) -> void:
	draw_line(Vector2(cx - half_w - skew, y), Vector2(cx + half_w - skew, y), col, w)


func _text_w(f: Font, t: String, sz: int) -> float:
	return f.get_string_size(t, 0, -1, sz).x


func _draw_ink(f: Font, pos: Vector2, txt: String, sz: int, col: Color,
			   shadow: float = 0.45) -> void:
	draw_string(f, pos + Vector2(1.5, 1.5), txt, 0, -1, sz,
		Color(0, 0, 0, shadow * col.a))
	draw_string(f, pos, txt, 0, -1, sz, col)


func _draw_ghost_number(pos: Vector2, txt: String, sz: int, accent: Color,
						a: float) -> void:
	draw_string_outline(font_heavy, pos, txt, 0, -1, sz, 5,
		Color(accent.r, accent.g, accent.b, 0.90 * a))
	draw_string(font_heavy, pos, txt, 0, -1, sz, Color(1, 1, 1, 0.95 * a))


func _draw_result_backdrop(vp: Vector2, a: float) -> void:
	## Soft light backdrop, matching the intro screen, with faint cover art.
	draw_rect(Rect2(Vector2.ZERO, vp), Color(0.957, 0.949, 0.976, a))
	var bg_rect: Rect2 = _fit_texture_rect(RES_BG_ART, Rect2(Vector2.ZERO, vp), true)
	draw_texture_rect(RES_BG_ART, bg_rect, false, Color(1, 1, 1, 0.16 * a))
	for i in 12:
		var f: float = float(i) / 12.0
		draw_circle(Vector2(vp.x * 0.5, vp.y * 0.52), vp.x * (0.16 + 0.48 * f),
			Color(0.62, 0.55, 0.82, 0.014 * (1.0 - f) * a))


func _fit_texture_rect(tex: Texture2D, box: Rect2, cover: bool) -> Rect2:
	## Scale a texture into `box`, preserving aspect ratio.
	var ts: Vector2 = tex.get_size()
	if ts.x <= 0.0 or ts.y <= 0.0:
		return box
	var s: float = maxf(box.size.x / ts.x, box.size.y / ts.y) if cover \
		else minf(box.size.x / ts.x, box.size.y / ts.y)
	var ds: Vector2 = ts * s
	return Rect2(box.position + (box.size - ds) * 0.5, ds)


# One restrained palette for the whole results screen.
const RES_BAR      := Color(0.145, 0.118, 0.235)   # header / footer bars
const RES_BANNER   := Color(0.243, 0.196, 0.376)   # song title banner
const RES_ACCENT   := Color(0.443, 0.325, 0.706)   # single purple accent
const RES_INK_DARK := Color(0.145, 0.125, 0.212)   # primary text on light
const RES_INK_SOFT := Color(0.451, 0.427, 0.529)   # secondary text on light
const RES_PANEL_BG := Color(0.639, 0.612, 0.714)   # score parallelogram
const RES_PANEL_INK := Color(0.192, 0.153, 0.322)  # dark purple on the panel

const RES_BG_ART: Texture2D = preload("res://assets/game_loss.png")

# Judgement value colours (labels stay neutral).
const VAL_PERFECT_TOP := Color(0.66, 0.34, 1.00)   # intro-logo purple
const VAL_PERFECT_BOT := Color(0.20, 0.72, 1.00)   # intro-logo blue
const VAL_PERFECT_A := Color(0.35, 0.68, 1.00)   # blue
const VAL_PERFECT_B := Color(0.62, 0.45, 0.96)   # purple
const VAL_PERFECT_C := Color(0.38, 0.88, 0.64)   # green
const VAL_EARLY     := Color(0.24, 0.62, 1.00)   # blue
const VAL_LATE      := Color(1.00, 0.32, 0.80)   # pink
const VAL_MISS      := Color(0.90, 0.26, 0.28)   # red


func _draw_grad_text(f: Font, pos: Vector2, txt: String, sz: int,
					 stops: Array, alpha: float) -> void:
	## Per-character gradient across an arbitrary list of colour stops.
	var x: float = pos.x
	var n: int = txt.length()
	for i in n:
		var ch: String = txt[i]
		var tt: float = float(i) / maxf(1.0, float(n - 1))
		var seg: float = tt * float(stops.size() - 1)
		var i0: int = clampi(int(floor(seg)), 0, stops.size() - 1)
		var i1: int = clampi(i0 + 1, 0, stops.size() - 1)
		var cc: Color = Color(stops[i0]).lerp(Color(stops[i1]), seg - float(i0))
		draw_string(f, Vector2(x, pos.y), ch, 0, -1, sz,
			Color(cc.r, cc.g, cc.b, alpha))
		x += _text_w(f, ch, sz)


func _vgrad_item(i: int) -> RID:
	## Pool of child canvas items, each clipped to one horizontal band.
	## draw_string() has no clip of its own, so a real top-to-bottom gradient
	## needs the text drawn once per band into an item that is clipped to it.
	while _vgrad_items.size() <= i:
		var it: RID = RenderingServer.canvas_item_create()
		RenderingServer.canvas_item_set_parent(it, get_canvas_item())
		_vgrad_items.append(it)
	return _vgrad_items[i]


func _vgrad_clear_all() -> void:
	for it in _vgrad_items:
		RenderingServer.canvas_item_clear(it)


func _draw_vgrad_text(f: Font, pos: Vector2, txt: String, sz: int,
					  c_top: Color, c_bot: Color, alpha: float) -> void:
	## Smooth top-to-bottom gradient across the whole string.
	if txt.is_empty() or alpha <= 0.01:
		return
	var w: float = _text_w(f, txt, sz)
	var asc: float = f.get_ascent(sz)
	var desc: float = f.get_descent(sz)
	var top: float = pos.y - asc
	var h: float = asc + desc
	var bands: int = 28
	for i in bands:
		var it: RID = _vgrad_item(i)
		RenderingServer.canvas_item_clear(it)
		var y0: float = top + h * float(i) / bands
		var bh: float = h / bands + 0.6
		RenderingServer.canvas_item_set_custom_rect(it, true,
			Rect2(pos.x - 3.0, y0, w + 6.0, bh))
		RenderingServer.canvas_item_set_clip(it, true)
		var tt: float = (float(i) + 0.5) / bands
		var cc: Color = c_top.lerp(c_bot, tt)
		f.draw_string(it, pos, txt, HORIZONTAL_ALIGNMENT_LEFT, -1, sz,
			Color(cc.r, cc.g, cc.b, alpha))


func _notification(what: int) -> void:
	if what == NOTIFICATION_PREDELETE:
		for it in _vgrad_items:
			RenderingServer.free_rid(it)
		_vgrad_items.clear()


func _fill_hex_gradient(cx: float, cy: float, half_w: float, half_h: float,
						flat: float, c_top: Color, c_bot: Color,
						alpha: float) -> void:
	## Fill an elongated hexagon with a TOP-TO-BOTTOM gradient by slicing it
	## into horizontal strips.  The shape's half-width shrinks linearly from
	## half_w at the vertical centre to half_w * flat at the top and bottom
	## edges, so each strip is a simple trapezoid.
	var slices: int = 40
	for i in slices:
		var y0: float = cy - half_h + (2.0 * half_h) * float(i) / slices
		var y1: float = cy - half_h + (2.0 * half_h) * float(i + 1) / slices
		var hw := func(y: float) -> float:
			var k: float = clampf(absf(y - cy) / maxf(half_h, 0.001), 0.0, 1.0)
			return lerpf(half_w, half_w * flat, k)
		var w0: float = hw.call(y0)
		var w1: float = hw.call(y1)
		var tt: float = (float(i) + 0.5) / slices
		var cc: Color = c_top.lerp(c_bot, tt)
		draw_colored_polygon(PackedVector2Array([
			Vector2(cx - w0, y0), Vector2(cx + w0, y0),
			Vector2(cx + w1, y1), Vector2(cx - w1, y1),
		]), Color(cc.r, cc.g, cc.b, alpha))


func _draw_results(vp: Vector2) -> void:
	var t: float = 1.0 if finish_ui < 0.0 else ui_time - finish_ui
	var a: float = smoothstep(0.30, 1.00, t)
	if a <= 0.005:
		return

	var stagger := func(i: float) -> float:
		return smoothstep(0.30 + i * 0.07, 0.95 + i * 0.07, t)

	var g: Array = _grade()
	var gtxt: String = String(g[0])
	var pct: float = float(g[2])
	var cx: float = vp.x * 0.5

	_draw_result_backdrop(vp, a)

	# ── Bars ─────────────────────────────────────────────────────────────
	var s0: float = stagger.call(0.0)
	var head_h: float = maxf(34.0, vp.y * 0.062)
	var ban_h: float = maxf(50.0, vp.y * 0.105)
	var bot_h: float = maxf(32.0, vp.y * 0.062)
	var ban_y: float = head_h
	var body_y: float = ban_y + ban_h
	var bot_y: float = vp.y - bot_h

	draw_rect(Rect2(Vector2.ZERO, Vector2(vp.x, head_h)),
		Color(RES_BAR.r, RES_BAR.g, RES_BAR.b, 0.97 * s0))
	draw_string(font_thin, Vector2(28.0, head_h * 0.66), "Result", 0, -1, 18,
		Color(0.92, 0.90, 0.96, 0.92 * s0))

	draw_rect(Rect2(Vector2(0.0, ban_y), Vector2(vp.x, ban_h)),
		Color(RES_BANNER.r, RES_BANNER.g, RES_BANNER.b, 0.96 * s0))
	var ts: int = 30
	draw_string(font_bold,
		Vector2(cx - _text_w(font_bold, song_title, ts) * 0.5, ban_y + ban_h * 0.66),
		song_title, 0, -1, ts, Color(0.96, 0.95, 0.99, 0.98 * s0))

	# ── Chibi on the right ───────────────────────────────────────────────
	var s1: float = stagger.call(1.0)
	var art_h: float = minf(vp.x * 0.20, (bot_y - body_y) * 0.50)
	var art_cy: float = body_y + (bot_y - body_y) * 0.58

	# The source art has a lot of empty margin, so the box is oversized to
	# make the character itself read at a decent size.
	var chibi: Texture2D = _result_art(gtxt)
	var ch_h: float = art_h * 2.05
	var ch_box := Rect2(Vector2(vp.x * 0.505, art_cy - ch_h * 0.5 - art_h * 0.30),
		Vector2(ch_h * 1.15, ch_h))
	var ch_rect: Rect2 = _fit_texture_rect(chibi, ch_box, false)
	ch_rect.position.x += (1.0 - s1) * 40.0
	draw_texture_rect(chibi, ch_rect, false, Color(1, 1, 1, s1))

	# ── Score column, shifted left of centre ─────────────────────────────
	# The chibi sits right of it, so the pair reads as centred overall.
	var s2: float = stagger.call(2.0)
	var col_cx: float = vp.x * 0.355
	var col_w: float = vp.x * 0.30
	var skew: float = 13.0
	var half_w: float = col_w * 0.64

	var sc_y: float = body_y + (bot_y - body_y) * 0.07
	var sc_h: float = 96.0
	_skew_band(col_cx, half_w, sc_y, sc_h, skew,
		Color(RES_PANEL_BG.r, RES_PANEL_BG.g, RES_PANEL_BG.b, 0.42 * s2))

	# Thin decorative line cluster at the panel's lower-left corner
	var lc: Color = Color(RES_ACCENT.r, RES_ACCENT.g, RES_ACCENT.b, 0.55 * s2)
	for i in 5:
		var off_y: float = sc_h - 6.0 - float(i) * 7.0
		var seg: float = 46.0 - float(i) * 7.0
		var fade: float = 1.0 - float(i) * 0.16
		var lx: float = col_cx - half_w - skew + (sc_h - off_y) * (skew * 2.0 / sc_h)
		draw_line(Vector2(lx - seg - 10.0, sc_y + off_y),
			Vector2(lx - 10.0, sc_y + off_y),
			Color(lc.r, lc.g, lc.b, lc.a * fade), 1.4)

	var sc_txt: String = _fmt_score(score)
	var sc_sz: int = 46
	draw_string(font_thin,
		Vector2(col_cx - _text_w(font_thin, sc_txt, sc_sz) * 0.5, sc_y + 52.0),
		sc_txt, 0, -1, sc_sz,
		Color(RES_PANEL_INK.r, RES_PANEL_INK.g, RES_PANEL_INK.b, 0.98 * s2))

	# Percentage — smaller, dark purple, inside the panel
	var ptxt: String = "%.2f%%" % (pct * 100.0)
	var p_sz: int = 22
	draw_string(font_thin,
		Vector2(col_cx - _text_w(font_thin, ptxt, p_sz) * 0.5, sc_y + 82.0),
		ptxt, 0, -1, p_sz,
		Color(RES_PANEL_INK.r, RES_PANEL_INK.g, RES_PANEL_INK.b, 0.88 * s2))

	# ── Grade letter with a split rule either side of it ─────────────────
	var s3: float = stagger.call(3.0)
	var gsz: int = 62 if gtxt.length() <= 2 else 44
	var gcy: float = sc_y + sc_h + 66.0
	var g_w: float = _text_w(font_heavy, gtxt, gsz)
	var bar_h: float = 4.0
	var gap: float = g_w * 0.5 + 11.0        # clear space around the letter
	var reach: float = g_w * 0.5 + 74.0      # outer end of each segment
	var rule_col := Color(RES_PANEL_INK.r, RES_PANEL_INK.g, RES_PANEL_INK.b,
		0.88 * s3)

	for side in [-1.0, 1.0]:
		var x_in: float = col_cx + side * gap
		var x_out: float = col_cx + side * reach
		draw_rect(Rect2(Vector2(minf(x_in, x_out), gcy - bar_h * 0.5),
			Vector2(absf(x_out - x_in), bar_h)), rule_col)
		# Small hollow diamond (square on its diagonal, vertex down) sitting
		# flush against the outer end of each segment, stroked at the same
		# weight as the line so the two read as one piece.
		var d: float = 7.0
		var dc: float = x_out + side * d
		draw_polyline(PackedVector2Array([
			Vector2(dc, gcy - d), Vector2(dc + d, gcy),
			Vector2(dc, gcy + d), Vector2(dc - d, gcy),
			Vector2(dc, gcy - d),
		]), rule_col, bar_h)

	var g_col: Color = Color(g[1])
	var g_glow: Color = Color(g[3])
	var g_pos := Vector2(col_cx - g_w * 0.5, gcy + gsz * 0.36)
	# Two wide, very faint outline passes read as a soft halo rather than a
	# hard stroke; the letter itself is drawn in the heavy weight on top.
	draw_string_outline(font_heavy, g_pos, gtxt, 0, -1, gsz, 14,
		Color(g_glow.r, g_glow.g, g_glow.b, 0.16 * s3))
	draw_string_outline(font_heavy, g_pos, gtxt, 0, -1, gsz, 7,
		Color(g_glow.r, g_glow.g, g_glow.b, 0.22 * s3))
	draw_string(font_heavy, g_pos, gtxt, 0, -1, gsz,
		Color(g_col.r, g_col.g, g_col.b, 0.99 * s3))

	# ── Judgement counts ─────────────────────────────────────────────────
	var rows := [
		["PERFECT", counts["PERFECT"]],
		["EARLY", counts["EARLY"]],
		["LATE", counts["LATE"]],
		["MISS", counts["MISS"]],
	]
	var val_stops := [
		[VAL_PERFECT_A, VAL_PERFECT_B, VAL_PERFECT_C],
		[VAL_EARLY, VAL_EARLY],
		[VAL_LATE, VAL_LATE],
		[VAL_MISS, VAL_MISS],
	]
	var axis: float = col_cx - 4.0
	var y_row: float = gcy + gsz * 0.36 + 48.0
	var lab_sz: int = 18
	var val_sz: int = 23
	var row_gap: float = 33.0
	for i in rows.size():
		var s4: float = stagger.call(4.0 + float(i) * 0.30)
		var lab: String = String(rows[i][0])
		var val: String = str(rows[i][1])
		var ry: float = y_row + float(i) * row_gap
		var dx: float = (1.0 - s4) * 18.0
		draw_string(font_thin,
			Vector2(axis + dx - _text_w(font_thin, lab, lab_sz), ry),
			lab, 0, -1, lab_sz,
			Color(RES_INK_SOFT.r, RES_INK_SOFT.g, RES_INK_SOFT.b, 0.95 * s4))
		if i == 0:
			# PERFECT: smooth top-to-bottom purple -> blue, as on the intro logo
			_draw_vgrad_text(font_bold, Vector2(axis + dx + 28.0, ry), val, val_sz,
				VAL_PERFECT_TOP, VAL_PERFECT_BOT, 0.97 * s4)
		else:
			_draw_grad_text(font_bold, Vector2(axis + dx + 28.0, ry), val, val_sz,
				val_stops[i], 0.97 * s4)

	# ── MAX COMBO, under the judgement list ──────────────────────────────
	var s5: float = stagger.call(5.6)
	var mc_y: float = y_row + float(rows.size()) * row_gap + 16.0
	draw_string(font_thin,
		Vector2(axis - _text_w(font_thin, "MAX COMBO", lab_sz), mc_y),
		"MAX COMBO", 0, -1, lab_sz,
		Color(RES_INK_SOFT.r, RES_INK_SOFT.g, RES_INK_SOFT.b, 0.95 * s5))
	draw_string(font_bold, Vector2(axis + 28.0, mc_y), str(best_combo),
		0, -1, val_sz,
		Color(RES_INK_DARK.r, RES_INK_DARK.g, RES_INK_DARK.b, 0.95 * s5))

	# ── Footer ───────────────────────────────────────────────────────────
	draw_rect(Rect2(Vector2(0.0, bot_y), Vector2(vp.x, bot_h)),
		Color(RES_BAR.r, RES_BAR.g, RES_BAR.b, 0.97 * a))
	var foot_col := Color(0.92, 0.90, 0.96, 0.90 * a)
	var retry_txt := "Retry  (R)"
	draw_string(font_thin, Vector2(28.0, bot_y + bot_h * 0.66), "Back  (ESC)",
		0, -1, 15, foot_col)
	draw_string(font_thin,
		Vector2(vp.x - 28.0 - _text_w(font_thin, retry_txt, 15), bot_y + bot_h * 0.66),
		retry_txt, 0, -1, 15, foot_col)
	if ImuInput.link_up:
		var f_txt := "flick UP to replay      flick DOWN to quit"
		draw_string(font_thin,
			Vector2(cx - _text_w(font_thin, f_txt, 13) * 0.5, bot_y + bot_h * 0.66),
			f_txt, 0, -1, 13, Color(0.72, 0.70, 0.80, 0.85 * a))