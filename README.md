# bing bang dream adventure !

a rhythm game you play by flicking a circuit board around!!

inspired by project sekai and beatsaber, uses my custom devboard for the imu-equipped controllers: https://github.com/darshg321/ultisense 


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
dashboard/    Python: the sensor dashboard, and the bridge that feeds the game
game/         the Godot 4.7 project
leaderboard/  a static page that reads the score file the game writes
```

The game never talks to the board directly, because Godot has no way to open a
serial port. `dashboard/game_bridge.py` reads the board over USB or WiFi, decides what counts as a flick, and posts each one to the game
over localhost.

## Running it

Flash the firmware once:

```bash
arduino-cli lib install ICM45605
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

Then leave the bridge running next to the game:

```bash
cd dashboard
pip install -r requirements.txt
python game_bridge.py                       # finds the board on USB
python game_bridge.py --host 192.168.1.50   # or over WiFi
python game_bridge.py --demo                # no board at all, fake flicks
```

### The dashboard

```bash
cd dashboard
python main.py                 # or --port COM14, or --host 192.168.1.50
```

There's
an easy mode on the Calibrate tab that walks you through it in about four
minutes: put the board down, rest it on each of its six sides, wave it around,
shove it across the table once. After that the axes are named and everything
else works properly.

</content>
