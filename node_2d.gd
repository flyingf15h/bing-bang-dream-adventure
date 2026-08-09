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


func _on_resize() -> void:
	centre = get_viewport_rect().size * 0.5
	if video:
		video.size = get_viewport_rect().size


func _setup_video() -> void:
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

	var prog: float = _progress()
	for sp in sparks:
		sp["phase"] = float(sp["phase"]) + delta * float(sp["speed"])
		sp["x"] = float(sp["x"]) + delta * 0.045 * float(sp["drift"])
		if float(sp["x"]) > prog + 0.02 or float(sp["x"]) < 0.0:
			sp["x"] = randf() * maxf(0.02, prog)
			sp["y"] = randf_range(-1.0, 1.0)


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

		if String(n["state"]) == "wait" and lead < -win_near / 1000.0:
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


func _judge_for(err_ms: float) -> String:
	var a: float = absf(err_ms)
	if a <= win_perfect:
		return "PERFECT"
	if a <= win_near:
		return "EARLY" if err_ms < 0.0 else "LATE"
	return ""


func _try_hit(sector: int, lag_ms: float = 0.0) -> void:
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
		return
	var verdict: String = _judge_for(best_err)
	if verdict == "":
		return

	ring_flash[sector] = 1.0
	if float(best["hold"]) > 0.0:
		best["state"] = "holding"
		best["release_at"] = -1.0
		_award(best, verdict)
		_popup(best, verdict, _verdict_colour(verdict))
	else:
		_finish(best, verdict)


func _on_tap(event: TapEvent) -> void:
	# Mouse/touch/IMU all funnel through TapInputBus and land here, reusing
	# the exact same _try_hit() scoring path as the A/S/D/J/K/L keys.
	if finished:
		_results_tap(event)
		return
	if autoplay or paused or not started:
		return

	# A flick from the IMU names its direction outright: the board is not
	# anywhere on screen, so there is no position to read an angle from.
	if event.has_direction():
		_try_hit(_nearest_sector(event.direction_deg), event.lag_ms)
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


func _finish(n: Dictionary, verdict: String) -> void:
	n["state"] = "done"
	n["judged"] = verdict
	n["hit_at"] = song_time
	_award(n, verdict)
	_popup(n, verdict, _verdict_colour(verdict))


func _award(n: Dictionary, verdict: String) -> void:
	counts[verdict] = int(counts.get(verdict, 0)) + 1
	if verdict == "MISS":
		combo = 0
		return
	var awards: float = 2.0 if float(n["hold"]) > 0.0 else 1.0
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

	var hint := "A S D / J K L    SPACE pause   R restart   F autoplay   ESC title   , . speed %.1fx   [ ] ; ' latency %+.0fms" % [speed_mult, audio_offset_ms]
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
