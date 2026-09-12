# bing bang dream adventure !

A Bad Apple!! rhythm game synced to custom IMU-based flick controllers (6-direction input). Flick the notes with the correct time and direction to see the happiest ending for the girls :)   

Inspired by project sekai and beatsaber, uses a custom devboard for the IMU-equipped controllers: https://github.com/darshg321/ultisense 


### Gameplay
<img width=90% alt="gameplay" src="https://github.com/user-attachments/assets/60197b6f-13b8-43c3-ab1e-5cd978b65b20" />

### Intro screen
<img width=90% alt="intro screen" src="https://github.com/user-attachments/assets/1a9cd251-4646-421f-840c-63d1d7461880" />

### Beatmap selection
<img width=90% alt="beatmap selection" src="https://github.com/user-attachments/assets/ba1901ba-3d48-42c2-ab56-feaaa6430755" />

### Results
<img width=90% alt="results" src="https://github.com/user-attachments/assets/6856caf3-e814-4170-a9eb-1257b985db98" />

### Leaderboard
<img width=90% alt="leaderboard" src="https://github.com/user-attachments/assets/3498af87-1977-4f82-af06-008c83f14767" />

## What's in here

```
firmware/     the Arduino sketch that runs on the board
bridge/       Python: the headless service that feeds the board's IMU into the game
game/         the Godot 4.7 project
leaderboard/  a static page that reads the score file the game writes
```

The game never talks to the board directly, because Godot has no way to open a
serial port. `bridge/run_bridge.py` reads the board over USB or WiFi, decides
what counts as a flick, and posts each one to the game over localhost.

## Running it

Flash the firmware once:

```bash
arduino-cli lib install ICM45605
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

Then leave the bridge running next to the game:

```bash
cd bridge
pip install -r requirements.txt
python run_bridge.py                       # finds the board(s) on USB
python run_bridge.py --host 192.168.1.50   # or over WiFi
python run_bridge.py --demo                # no board at all, fake flicks
```

Calibration is being moved into the bridge as a headless service, driven from
inside the game rather than from a separate desktop dashboard -- see
`PLAN.md` for where that stands.

</content>
