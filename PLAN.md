# v2 — seamless play

Branch: `v2-seamless`. This is a **breaking rewrite of everything around the
game**. Compatibility with v1 layouts, flags, file paths and the PySide6
dashboard is explicitly not a goal. Restructure freely.

The one thing that must not change is **how the game plays**. See
"Gameplay invariants" at the bottom before touching anything, and re-read it at
the end of every step.

---

## The idea

One Python process owns all hardware. The Godot game owns all UI. Nothing else
exists.

Today that is not true: calibration lives in a separate 10,000-line PySide6
dashboard, the bridge is a second entry point, and the game reaches the board
through neither. The fix is not to port calibration into GDScript — the maths is
numpy least-squares and does not belong there. The fix is to make the bridge a
**service** that exposes calibration as commands, and make the game its only
client.

### Target layout

```
firmware/                  unchanged this release
bridge/
  run_bridge.py            <- THE one file a user runs
  bbda/
    link.py                serial + UDP transports, no Qt
    protocol.py            v2 wire format, one definition both sides read
    motion.py              flick detector          (behaviour frozen)
    fusion.py              orientation             (behaviour frozen)
    calibration.py         calibration maths       (behaviour frozen)
    calseq.py              headless calibration state machine  (new)
    service.py             was gamebridge.py: streaming + command loop
  requirements.txt         pyserial, numpy. nothing else.
game/
  autoload/Settings.gd     every setting: imu, audio, gameplay
  autoload/ImuInput.gd     wire v2 client
  autoload/TapInputBus.gd  unchanged
  scenes/SettingsOverlay.tscn
  scenes/CalibrationWizard.tscn
  profiles/default.json    the known-good tuning, committed
build/                     BingBangDreamAdventure.exe + bridge + README
tools/build_windows.ps1
```

### Deleted outright

`dashboard/bbda/app.py`, `wizard.py`, `view3d.py`, `widgets.py`, `theme.py`,
`guide.py`, `main.py`, `flick_check.py`, and the PySide6 / pyqtgraph / PyOpenGL
dependencies.

Before deleting `wizard.py`, **read it** — its step sequencing and its
settle/spread thresholds (`GRAVITY_SPREAD_G`, `SETTLE_NAG_SAMPLES`, the `STEPS`
list at line 135) are the specification for Step 4. All of it stays reachable on
`main` and in git history; nothing is being lost, only unshipped.

---

## Step 1 — Restructure, and cut Qt out for good

**Why:** `bbda/link.py` imports `QObject`/`Signal` for five signals, which drags
PySide6 into a headless bridge. The consumer surface is tiny — five `.connect`
calls in `app.py:1173-1177`, three in `gamebridge.py:750-752`, one in
`flick_check.py:449` — and `app.py` and `flick_check.py` are both being deleted.
So there is nothing left to be compatible with.

### 1a. Move files

`git mv dashboard bridge`, then delete the GUI modules listed above. Rename
`bbda/gamebridge.py` → `bbda/service.py` and `game_bridge.py` → `run_bridge.py`.
Move `bridge/tests/` along with it — those tests are the regression guard for
the gameplay invariants and must keep running.

### 1b. New `bridge/bbda/signals.py`

```python
class Signal:
    """Declared on the class, bound per instance — like a Qt Signal was."""
    def __set_name__(self, owner, name): self._name = name
    def __get__(self, instance, owner):
        if instance is None: return self
        bound = instance.__dict__.get(self._name)
        if bound is None:
            bound = _Bound(); instance.__dict__[self._name] = bound
        return bound

class _Bound:
    def __init__(self): self._slots = []
    def connect(self, slot): self._slots.append(slot)
    def emit(self, *args):
        for slot in tuple(self._slots): slot(*args)

class Emitter:
    def __init__(self, *a, **k): pass
```

**Emission stays synchronous, on the reader thread.** That is deliberate and it
is the whole reason Qt could go: there are no widgets left to be thread-unsafe
about, and the alternative — queueing records for the main loop to drain — would
add up to the main loop's 50 ms sleep (`run_bridge.py:538`) to every flick. In a
rhythm game that is the difference between a hit and a miss.

### 1c. Rewrite the imports

- `link.py`: `from .signals import Emitter, Signal`; `class Link(Emitter)`.
- `service.py`: drop the `Qt` import; the three `.connect(fn, Qt...DirectConnection)`
  calls become plain `.connect(fn)`.

### 1d. `bridge/requirements.txt`

```
pyserial>=3.5
numpy>=1.24
```

**Check:** `python -m pytest bridge/tests/` passes. In a venv with *only* those
two packages, `python bridge/run_bridge.py --demo` prints lane flicks.
`pip list` in that venv must not contain PySide6.

---

## Step 2 — Protocol v2, defined once

**Why:** the wire format is currently implied by scattered `record.get(...)`
calls on both sides, and Step 4 roughly doubles the number of message types. One
definition, read by both sides, or they will drift.

### 2a. `bridge/bbda/protocol.py`

Module-level constants and small builder functions for every record. Bump
`WIRE_VERSION` to `2`. Existing types keep their current field names and
meanings: `hello`, `status`, `bye`, `flick`, `motion`, `refused`, `config`,
`front_suggestion`, `rest`, `bias_written`, `board_cal`.

New types this release:
- `scan` — serial ports and which look like boards.
- `cal_state` — calibration step, progress 0-1, prompt text, per-axis quality.
- `cal_done` — accepted or rejected, with the residuals.
- `transport` — what a board is now reached on, after a switch.

New commands the game may send: `scan`, `transport`, `cal_start`, `cal_advance`,
`cal_cancel`, `cal_save`.

**Prompt text is authored on the bridge, not in GDScript.** The prompts belong
next to the thresholds that decide when a step passes; splitting them means
every threshold tweak needs an edit in two languages.

### 2b. Mirror it in `game/autoload/wire.gd`

A plain constants file with the same type and field names, so the Godot side
never spells a key as a bare string literal. Add a comment on both files
pointing at the other.

**Check:** `grep -c '"type"' bridge/bbda/service.py` returns 0 — every record is
built through `protocol.py`.

---

## Step 3 — Transport: find everything, switch live, stay fast

### 3a. Zero-flag default

`plan_boards()` (`run_bridge.py:246`) currently ends by opening a single board
with an empty target. Replace that fallback with `find_all_board_ports()`:
0 found → retry loop as now; 1 → solo board, hand `""`; 2+ → first two become
`left` and `right`. Delete `--two-boards`; it is now the default.

### 3b. Fix WiFi lag

`UdpLink.connect()` sets `SO_RCVBUF` to 1 MB (`link.py:480`). That was right for
a dashboard plotting every sample and is wrong here: at ~200 Hz it is seconds of
queued motion, and it presents as *flicks landing late*, not as a broken link.
The firmware is already fine — `bbda_imu.ino:560` does `WiFi.setSleep(false)`,
which is the usual culprit — so this is the remaining one.

Set `SO_RCVBUF` to 64 KB, and in `_read_loop` (line 530) drain to the newest
datagram before feeding: after a successful `recvfrom`, loop non-blocking
(`except BlockingIOError: break`) keeping only the last chunk, then restore the
timeout. Feed only that one.

Leave `SerialLink` alone — it is lossless and already prompt.

### 3c. Switching, from the game

Handle `transport` in the command loop (`service.py:433`): close that board's
link, reopen via `make_link()`, update `bridge.reconnect_target` so a later
dropout returns on the **new** transport, and emit a `transport` record. The
existing `hello` already carries the transport name, and
`ImuInput._handle_hello` (line 419) already stores it.

### 3d. Make latency visible

`peak_transport_ms` is already tracked for `--monitor` (`run_bridge.py:556`).
Put it in the once-a-second `status` record as `wire_ms`. Step 6 displays it.
Without a number on screen, "does wireless feel laggy" is unanswerable.

**Check:** no flags, two boards on USB → blue and pink both play. Switch one to
WiFi from the game; it reconnects without restarting the bridge. `wire_ms` holds
under ~15 ms and **does not climb** over two minutes — a climbing number means
3b is not working.

---

## Step 4 — Calibration as a service

**Why:** this is what makes the dashboard deletable. Calibration is numpy
least-squares in `calibration.py` (804 lines, frozen) driven by a step sequence
in `wizard.py` (deleted). Move the sequence to the bridge, headless; let the game
render it.

### 4a. `bridge/bbda/calseq.py`

A state machine, one instance per board, no I/O of its own:

```python
class CalSequence:
    def start(self, kind: str) -> None      # "full" | "quick"
    def feed(self, sample: Sample) -> None  # every sample while running
    def advance(self) -> None               # player pressed Continue
    def cancel(self) -> None
    def state(self) -> dict                 # -> a cal_state record
```

Steps, from `wizard.py:135`: rest → six sides → wave → which-way → check → save.
Port the acceptance thresholds verbatim (`GRAVITY_SPREAD_G`,
`SETTLE_NAG_SAMPLES`) — they are tuned against real hardware and guessing new
ones means a wizard that accepts a bad calibration.

Auto-advance when a step's own criterion is met; `advance()` is only for the
steps that wait on the player. Emit `cal_state` at ~10 Hz so the game can show a
live quality bar rather than a spinner.

### 4b. Wire it into the service

`cal_start` / `cal_advance` / `cal_cancel` drive the machine; samples are fed
from the existing sample path. `cal_save` calls the existing
`to_device_commands()` (`calibration.py:466`) and sends `cal ...` + `cal save`
to the board over whichever link is open — unchanged behaviour, new caller.

**While a sequence runs, that board must not emit `flick` records.** The player
is waving the board through calibration poses; every one of those would
otherwise be an input. The game's own `capture_only` flag
(`ImuInput.gd:68`) is not enough, because it only gates the input bus.

**Check:** with the game not running, a scripted client that sends `cal_start`
and `cal_advance` over the control port can complete a full calibration and
write it to the board. Prove it there before building any UI on it.

---

## Step 5 — One Settings autoload, one profile file

**Why:** `ImuSettings.gd` already does this well for IMU tuning (export/import
JSON, `edited` bookkeeping, migrations). It needs to absorb audio and gameplay,
and `audio_offset_ms` needs a home at all — it is currently an `@export` on
`Gameplay.gd:12`, nudged by the `[ ] ; '` keys at lines 1085-1091, and lost on
quit.

### 5a. `ImuSettings.gd` → `Settings.gd`

Rename the autoload in `project.godot` and at every call site. Keep the internal
structure; it is sound. Add:

- `audio_offset_ms: float = -50.0` — into the `assist()` dictionary (line 200),
  which carries it through `save_settings`, `_read_assist`, `export_to` and
  `import_from` for free. **Verify all four**, do not assume.
  Clamp to ±300 in `_clamp_assist()`; add to `reset_to_defaults()`.
- `vol_master`, `vol_music`, `vol_sfx` — floats 0-1, default 1, new `[audio]`
  section.
- `FORMAT_VERSION` → `5`. v1 files are not migrated; this is a breaking release,
  so `_migrate()` may simply reset anything older and say so.

### 5b. `Gameplay.gd` reads Settings

Delete the `@export`. Read `Settings.audio_offset_ms` in `_ready()` and on
`changed`. The `[ ] ; '` keys call `Settings.set_assist("audio_offset_ms", ...)`,
which saves and emits. The audio path at lines 373-374 keeps using a local float,
so timing is untouched.

### 5c. `game/profiles/default.json`

The values currently known good, in `export_to()`'s schema (line 565):

```json
{
  "format": 5,
  "tuning": {"front": "+X", "on_threshold_dps": 110.0, "min_swing": 0.2,
             "min_margin": 0.0, "refractory_ms": 200.0,
             "sector_offset_deg": 30.0, "calibrated": true,
             "commit_fraction": 0.6},
  "assist": {"lane_tolerance_deg": 75.0, "timing_scale": 2.8,
             "audio_offset_ms": -50.0},
  "display": {"show_arrow": true, "colour_only_hits": true,
              "arrow_flicks_only": false},
  "aim": {"bearing_offset_deg": 0.0, "bearing_flip": false},
  "audio": {"vol_master": 1.0, "vol_music": 1.0, "vol_sfx": 1.0}
}
```

When `load_settings()` finds no saved file (line 430), import this first. A fresh
install then starts on the good values. Settings tab gets Load / Save As buttons
onto the same format, so a tuning can be carried between machines.

**Check:** delete the user settings file, launch, confirm the latency readout
(`Gameplay.gd:1860`) says `-50ms` and lane tolerance is 75. Change with `]`,
quit, relaunch — it survives. Export, edit the JSON by hand, import — it takes.

---

## Step 6 — The gear, and everything behind it

**Why:** `ImuDebugPanel` (1114 lines) is added ad-hoc in two scenes
(`Start.gd:45`, `Gameplay.gd:191`) and toggled by its own checkbox. In this
release it is **rewritten**, not reparented — its layout is a debug dump, and the
target is a settings menu a player can use.

### 6a. `SettingsOverlay` autoload

A `CanvasLayer` at a high `layer`, registered in `project.godot` after
`Settings`. Gear `TextureButton` anchored top-right; a hidden `PanelContainer`
with a `TabContainer`.

- Esc toggles it — but `Gameplay.gd` already binds Esc to "return to title", so
  the overlay must call `get_viewport().set_input_as_handled()` when it is open
  and only then.
- Pause with `get_tree().paused = true`, overlay set to `PROCESS_MODE_ALWAYS`.
  `Gameplay.gd` already has a pause key; route through that path rather than
  fighting it, so audio and chart resume together.

### 6b. Tabs

| Tab | Contents |
|---|---|
| **Audio** | Master / Music / SFX (Step 8) |
| **Controllers** | Per board: colour, link state, `wire_ms`, rate. USB/WiFi toggle. "Calibrate" → Step 7 |
| **Gameplay** | Note delay, lane tolerance, timing scale. Load / Save profile |
| **Advanced** | Detection floors, front axis, arrow display, raw record log — everything from the old panel, collapsed by default |

Delete both ad-hoc `add_child(ImuDebugPanel.new())` calls. Keep
`Settings.panel_open` as the overlay's remembered state.

**Check:** gear on every screen. Opening mid-song pauses cleanly; resuming does
not desync audio from notes. Nothing debug-shaped is reachable without it.

---

## Step 7 — Calibration wizard, in-game

`game/scenes/CalibrationWizard.tscn`, opened from the Controllers tab. It is a
**thin renderer** over Step 4: display the prompt from `cal_state`, draw the
quality bar from its progress, show a Continue button when the state says it is
waiting. No thresholds and no step logic in GDScript — if a rule is being decided
here, it is in the wrong file.

Run one board at a time, named by colour (`ImuInput.hand_label()`, line 362).
After the board-level sequence, finish with the two game-level steps that already
exist as bridge commands:

- **Front axis** — `learn_front(0.0)` after "flick straight up". Set
  `ImuInput.capture_only = true` first; line 68 documents exactly this hazard
  (on the title screen a flick otherwise presses Start and leaves the wizard).
- **Direction check** — four flicks solving rotation + mirror. `ImuDebugPanel`
  already implements this at lines 343-362; **extract the solver to a shared
  static function** rather than writing a second one. Call
  `Settings.set_aim(offset, flip, hand)`.

Clear `capture_only` on every exit path including cancel and error. Leaving it
set makes the board dead to the game with nothing on screen to say why.

**Check:** from a deleted settings file and an uncalibrated board, a player who
has never seen the dashboard completes both boards with mouse + controllers, then
plays a chart with correct lanes and no aim drift.

---

## Step 8 — Audio buses and volume

`game/default_bus_layout.tres` with `Master`, `Music`, `SFX`; referenced from
`project.godot` under `[audio]`. `Gameplay.gd:159` sets `player.bus = "Music"`.

Apply as `AudioServer.set_bus_volume_db(idx, linear_to_db(v))`, plus
`set_bus_mute(idx, v <= 0.001)` — `linear_to_db(0.0)` is `-inf` and some drivers
handle it badly, so mute explicitly at zero.

**Check:** Master to 0 mid-song silences audio while notes keep scrolling.
Survives a relaunch.

---

## Step 9 — Windows binary

**Start 9a now if anything is running in parallel** — it is slow and it hard-
blocks every build.

### 9a. Export templates

`%APPDATA%\Godot\export_templates\` is **empty**. Open
`C:\Users\darsh\Downloads\stuff\Godot_v4.7.1-stable_win64.exe\Godot_v4.7.1-stable_win64.exe`
→ Editor → Manage Export Templates → Download and Install. Must match the editor
version exactly. Verify `4.7.1.stable\windows_release_x86_64.exe` exists.

### 9b. `game/export_presets.cfg`

Create via Project → Export → Add → Windows Desktop, then commit it.

- Export path `../build/BingBangDreamAdventure.exe`, embed PCK on.
- **Include filter must cover `*.json`.** Godot drops unknown extensions, which
  would silently strip `game/charts/*.json` and the Step 5c profile. This is the
  single most likely cause of "runs in the editor, broken as a binary".
- Confirm the `.ogv` video and `.ogg` audio survive — play a full chart in the
  exported binary, not just in the editor.

### 9c. `tools/build_windows.ps1`

```powershell
$godot = "C:\Users\darsh\Downloads\stuff\Godot_v4.7.1-stable_win64.exe\Godot_v4.7.1-stable_win64_console.exe"
New-Item -ItemType Directory -Force build | Out-Null
& $godot --headless --path game --export-release "Windows Desktop" ..\build\BingBangDreamAdventure.exe
if ($LASTEXITCODE -ne 0) { throw "export failed ($LASTEXITCODE)" }
```

Use the `_console.exe` variant. The plain one detaches and swallows export
errors, so a failed build looks like a silent success.

### 9d. What ships

`build/` holds the `.exe`, `run_bridge.py`, the `bbda/` package,
`requirements.txt`, and a README reading in full:

```
1. pip install -r requirements.txt
2. python run_bridge.py
3. run BingBangDreamAdventure.exe
```

**Check:** on a machine with no Godot, no repo and no Python packages, those
three lines give a playable game with two working controllers.

---

## Gameplay invariants

Everything above changes how the game is *configured, calibrated and shipped*.
None of it may change how it *plays*. Frozen:

- **`bbda/motion.py`** — the flick detector. Moves file path, not behaviour.
- **`bbda/fusion.py`**, **`bbda/calibration.py`** — same.
- **`Gameplay.gd`** scoring, judgement windows, lane resolution, note spawning,
  `AIM_COST_MS_PER_DEG`. The only permitted edits are where
  `audio_offset_ms` comes from (Step 5b) and removing the debug panel
  `add_child` (Step 6a).
- **`ImuInput.bearing_to_game_angle()`** (line 671) — the wire convention.
  `test_gamebridge.py` restates it against the real lane layout precisely because
  an error here does not throw, it just puts every flick in the wrong lane.
- **The tuning values** in Step 5c. They are the known-good set; the profile
  exists to preserve them, not to revisit them.
- **`game/charts/*.json`**.

`bridge/tests/` is the mechanical guard. It must pass unchanged at the end of
every step — if a test needs editing to pass, the change went too far, unless
the edit is purely an import path from Step 1a.

---

## Order

Steps 1-4 are backend and each is verifiable without the game. Step 5 is small
and unblocks 6-8. Step 6 is the biggest single piece and 7 and 8 hang off it.
Step 9 is last, except 9a which should be started immediately.

Risk, highest first:

1. **Step 4** — the calibration sequence is the most intricate logic being moved,
   and a wizard that accepts a bad calibration fails silently, days later, as
   "the controller feels wrong". Port the thresholds verbatim.
2. **Step 6a** — pause semantics against a running song.
3. **Step 3b** — get the drain wrong and wireless is worse than before, not
   better.
4. **Step 9b** — export filters silently dropping charts.
