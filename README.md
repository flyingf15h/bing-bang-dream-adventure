# bing-bang-dream-adventure

Firmware and a desktop dashboard for a custom ESP32-S3 board carrying a TDK InvenSense ICM-45605** 6-axis IMU and a QST QMC6309 3-axis magnetometer.

The firmware exposes every feature both parts implement over a readable line
protocol, carried over USB serial and UDP at the same time. The dashboard
consumes that stream, fuses it into a live 3D orientation, and calibrates and
tunes the IMU.

**[docs/USAGE.md](docs/USAGE.md) documents everything in full.** This page is
the overview and the record of what was measured on real hardware.

---

## Hardware

| Signal | Net | Notes |
|---|---|---|
| I²C SDA | `GPIO8` | shared by both devices |
| I²C SCL | `GPIO9` | 400 kHz |
| ICM-45605 `INT1` | `GPIO17` | push-pull, active high, pulsed |
| ICM-45605 address | `0x68` | `AP_AD0` low |
| QMC6309 address | `0x7C` | fixed, the part has only one address |

## Running the game

**Firmware**

```bash
# One-time: install the vendor library for the IMU
arduino-cli lib install ICM45605

# The board enumerates as an ESP32-S3 Dev Module with USB CDC on boot
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" firmware/bbda_imu
arduino-cli upload  --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" -p COM14 firmware/bbda_imu
```

**Playing the game with the board**

Start the bridge next to the game and flick the board to hit notes:

```bash
cd dashboard
python game_bridge.py                       # over USB, finds the board itself
python game_bridge.py --host 192.168.1.50   # over WiFi instead
python game_bridge.py --demo                # no board: fake flicks, to test the game
python game_bridge.py --simulate-flicks     # real board, faked flicks, hands free
```

**Two boards, one per note colour.** Blue notes are the left hand, pink the
right; `any` and gold bonus notes take either. Both boards run in one process
and post to the same game port, but each keeps its own front axis and aim
correction — two mountings in two hands, and a correction fitted against one is
wrong for the other.

```bash
python game_bridge.py --board left=COM7 --board right=COM9
python game_bridge.py --board blue=COM7:+Y --board pink=COM9:-X   # per-board axis
python game_bridge.py --two-boards          # find both; the first gets blue
```

**Checking that flicks go where you aim them.** `python flick_check.py watch`
is a dial that follows the board live; `python flick_check.py aim` flicks each
lane and prints an error table. The in-game *Flick Test* chart
(`test_flicks.json`) is 32 slow notes with no music, for the same job with the
game running. Tick **IMU debug** on the title screen for the setup panel:
link state, live bearing and lane, front-axis helper, sensitivity sliders and
gyro bias measurement, all of which retune the running bridge live.

`docs/GAME_INPUT.md` covers setup over either transport, the diagnostic screen
(`ImuTest.tscn`), tuning, and the ESP32-S3 USB CDC traps.

**Dashboard**

```bash
cd dashboard
pip install -r requirements.txt
python main.py                # or: python main.py --port COM14
```

Pick the port and press **Connect**. The dashboard switches the board to its
machine-readable CSV mode automatically and restores the human-readable mode
when it disconnects.

**Over WiFi instead.** Give the board credentials once, over USB, and it
prints the address to connect to:

```
wifi ssid My Network
wifi pass hunter2
wifi connect          ->  OK wifi connected, ip 192.168.1.50, -47 dBm, udp port 3333
wifi auto on          # and join by itself at every boot from now on
```

Switch the dashboard's **Link** selector to *WiFi (UDP)*, type that address,
and connect. Nothing has to be configured on the board for it to know where to
send: it streams to whichever address last sent it a command, and connecting
is a command. Output goes to serial and UDP simultaneously, so a serial
monitor keeps working while the dashboard drives the board over the network.

---

## Calibration

```
gyro_out  = gyro_raw - gyro_bias
accel_out = (accel_raw - accel_bias) * accel_scale
mag_out   = mag_soft @ (mag_raw - mag_bias)
```

## Sources

- [ICM-45605 product page and datasheet, TDK InvenSense](https://invensense.tdk.com/products/motion-tracking/6-axis/icm-45605)
- [ICM-45605 / ICM-45686 user guide, AN-000478](https://invensense.tdk.com/wp-content/uploads/2024/07/AN-000478_ICM-45605-ICM-45686-User-Guide.pdf)
- [TDK InvenSense ICM45605 Arduino driver](https://github.com/tdk-invn-oss/motion.arduino.ICM45605)
- QST QMC6309 datasheet, document 13-52-22 Rev. C
