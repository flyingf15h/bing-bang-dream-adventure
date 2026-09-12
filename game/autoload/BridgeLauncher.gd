extends Node
## Starts and stops the bridge automatically, so playing this game is
## "plug in a board and run the .exe" and nothing else.
##
## build/bridge/bridge.exe is bridge/run_bridge.py + bbda/ frozen by
## tools/build_bridge.ps1 (--onedir, not --onefile -- see that script for
## why a single-file build would be the wrong choice here) -- no Python
## install needed on the machine that runs it. This node launches it the
## moment the game starts and kills it the moment the game closes, so a
## player never has to know the bridge exists as a separate process at all.
##
## Only in an exported build. In the editor the bridge is started by hand,
## same as it always was during development -- there is no bridge.exe next
## to the editor binary to launch, and a developer iterating on the bridge
## itself needs to run their own copy from source anyway.

var _pid: int = -1


func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	if OS.has_feature("editor"):
		return
	_launch()


func _launch() -> void:
	var exe_dir := OS.get_executable_path().get_base_dir()
	var bridge_path := exe_dir.path_join("bridge/bridge.exe")
	if not FileAccess.file_exists(bridge_path):
		# Not fatal -- the game plays fine on mouse, touch and keyboard with
		# no bridge at all, exactly as if one had not been started by hand
		# under the old workflow. This only happens if build/bridge/ was not
		# shipped alongside the game.
		push_warning("[bridge] " + bridge_path + " not found -- IMU controllers will not work")
		return
	_pid = OS.create_process(bridge_path, [], false)
	if _pid == -1:
		push_warning("[bridge] could not start " + bridge_path)


## Whichever of these fires first kills it; both are kept because neither
## alone is reliable for every way this process ends. _exit_tree() covers
## get_tree().quit() and the ordinary shutdown sequence, including the
## --quit-after this project's own headless tests use, none of which raise
## NOTIFICATION_WM_CLOSE_REQUEST. That notification exists for the one case
## _exit_tree() reaches too late to matter for -- killing the bridge the
## instant a close is known to be coming, rather than after whatever else
## unwinds first.
func _exit_tree() -> void:
	_kill()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_CRASH:
		_kill()


func _kill() -> void:
	if _pid != -1 and OS.is_process_running(_pid):
		OS.kill(_pid)
	_pid = -1
