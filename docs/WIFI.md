# Running the board over WiFi

Untethering the board is the whole point of it: a rhythm game played by
flicking something on the end of a USB cable is a poor experience. This is how
to set that up, what it costs, and the failure that is most likely to bite you
— which, on the board as it stands, it does.

---

## Before anything else: the radio is 2.4 GHz only

The ESP32-S3 has no 5 GHz radio. If your network's SSID is 5 GHz only, the
board cannot join it, and the error it gives (`could not join`) says nothing
about why.

Most routers broadcast one SSID on both bands, in which case it just works.
To check on Windows:

```powershell
netsh wlan show networks mode=bssid
```

Each SSID lists its BSSIDs with a channel. **Channel 1–14 is 2.4 GHz**;
anything from 36 up is 5 GHz. You need at least one low channel under your
SSID. Your PC being on the 5 GHz half of the same SSID is fine — same network,
different band.

---

## Setup

Do this once, over USB. Open a serial monitor on the board's port (any baud —
it is native USB, the number is decoration):

```
wifi ssid My Network
wifi pass hunter2
wifi connect
```

The SSID and password may contain spaces; everything after the subcommand is
taken literally. The board never echoes the password back, only a character
count, because that line would otherwise land in every serial log.

A successful join looks like:

```
NOTICE joining 'My Network', up to 12 s...
OK wifi connected, ip 10.2.219.181, -50 dBm, udp port 3333
```

That IP is what everything else needs. `wifi status` reprints it any time.

**Then, and only once the board has proven stable on WiFi** (see the power
section below), make it automatic:

```
wifi auto on
```

Credentials live in the board's own NVS and survive reflashing the sketch.
`wifi forget` clears them.

---

## Using it

**The game:**

```bash
cd dashboard
python game_bridge.py --host 10.2.219.181
```

**The dashboard:** switch the **Link** selector to *WiFi (UDP)*, type the
address, connect.

Nothing needs configuring on the board for either. It streams to **whichever
host last sent it a command**, and connecting sends one. A board left
streaming at a machine that has gone away redirects itself the moment another
one speaks to it, with no reboot.

Two consequences worth internalising, because both look like faults:

- **The dashboard and the bridge fight over the board.** Only one can have the
  stream. Opening the dashboard silently steals it from the bridge, which then
  sits at 0 Hz with its link still "open". Over USB the same clash is honest —
  the second program simply cannot open the port.
- **`udp.target none yet`** in `wifi status` is normal before anything has
  spoken to the board. It is not a misconfiguration.

To pin the board to one machine regardless:

```
udp host 10.2.220.2        # your PC's address
udp auto                   # back to following the last caller
```

---

## The failure you should expect: brownout

**This is what happened on this board.** Recorded here in full because the
symptoms point everywhere except the cause.

Bringing the radio up draws a lot of current — an ESP32-S3 transmitting peaks
in the region of 350–500 mA, in bursts of microseconds. If the supply cannot
deliver that, the rail sags, the brownout detector fires, and the chip resets.

What that looks like, in the order it appears:

| Stage | Symptom |
|---|---|
| USB only | perfect — hours at 200 Hz, nothing wrong |
| First UDP commands | works; short bursts are within budget |
| Sustained streaming | a few dozen samples, then it stops |
| `wifi auto on` set | the board reboots about once a second, for ever |

The last row is the dangerous one. With auto-join enabled, the reset lands
straight back in the join, so the board never stays up long enough to accept
the command that would turn it off. The serial port appears and disappears on
the same one-second cycle. Measured here: 44 port appearances in 90 seconds,
117 attempts to open it, not one long enough to get a command in.

**The fix is electrical, not software.** In rough order of likelihood:

1. **A shorter or thicker USB cable.** Long thin cables are the usual culprit;
   the voltage drop is in the cable, not the port.
2. **A different USB port** — rear ports on a desktop are fed better than
   front ones, and better than hubs.
3. **A powered hub**, or a supply into the board's own power input if it has
   one.
4. **Bulk capacitance across the board's 3V3 rail** — a few hundred µF close to
   the module rides out the transmit bursts. This is the standard fix on a
   board that browns out only when the radio is active.

A useful confirmation before you change anything: the board works flawlessly
on USB with the radio off, and fails only when it transmits. That pattern is
brownout and essentially nothing else — a genuinely broken radio or bad
credentials fails to *associate*, it does not reset the chip.

### Getting a boot-looping board back

The firmware now defends itself. It records an attempt in NVS before joining
and clears it on success, so **two boots that both died mid-join means the
third comes up with the radio off** and says so:

```
ERR wifi: two boots died while joining, so the radio is off this time.
    Almost always a supply that cannot meet the radio's transmit current:
    try a shorter or thicker USB cable, a different port, or a powered hub.
```

Your credentials and settings are untouched; `wifi connect` retries by hand
and `wifi auto off` stops it trying at boot.

If you are running firmware older than that guard, or the loop is tighter than
it can catch, build the rescue hatch:

```bash
arduino-cli compile --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" \
  --build-property "compiler.cpp.extra_flags=-DBBDA_SKIP_AUTOJOIN=1" \
  --output-dir /tmp/bbda_rescue firmware/bbda_imu
arduino-cli upload --fqbn "esp32:esp32:esp32s3:CDCOnBoot=cdc" \
  --input-dir /tmp/bbda_rescue -p COM5
```

That build ignores the stored auto-join setting. Boot it, send
`wifi auto off`, then flash the normal build back. Flashing works even in a
tight reset loop, because the ROM bootloader runs before the sketch does.

The alternative — erasing the NVS partition with `esptool erase_region 0x9000
0x5000` — also works and takes your calibration with it. Prefer the rescue
build.

---

## What WiFi costs

| | USB serial | WiFi |
|---|---|---|
| Loss | none | occasional dropped datagram |
| Latency | ~1 ms, steady | a few ms on a quiet network |
| On a busy network | unaffected | measured 69–319 ms ping jitter here |
| Setup | none | credentials, and an IP that may change |
| Power draw | low | high, in bursts — see above |
| Board is | tethered | free |

A dropped datagram costs at most one flick and never produces a *wrong* one:
detection runs over a window of samples and every record carries the device
timestamp, so loss shows up as a gap rather than as bad data. That is the
right trade for live telemetry and the wrong one for anything where a gap
matters — push calibrations over USB if you want certainty they arrived.

Note the third row. On a shared campus or office network, latency is dominated
by the network, not the board. Firmware already disables modem sleep
(`WiFi.setSleep(false)`), so the jitter you see is contention with everyone
else, and a quiet home network behaves far better.

---

## Troubleshooting

| Symptom | Likely cause | Try |
|---|---|---|
| `could not join`, status `NO_SSID_AVAIL` | 5 GHz-only SSID, or out of range | check for a 2.4 GHz BSSID |
| `could not join`, status `CONNECT_FAILED` | wrong password | `wifi pass` again |
| Joins, then resets seconds later | brownout | cable, port, powered hub, bulk caps |
| Reboots ~once a second | brownout plus `wifi auto on` | rescue build above |
| Link opens, 0 Hz, no samples | something else took the stream | close the dashboard |
| Samples arrive, then stop | brownout, or the board dropped off | `wifi status` over USB |
| Works over USB, never over WiFi | isolation on the network | ping the board's IP from the PC |
| IP changes between sessions | DHCP | pin with `udp host <pc-ip>`, or a DHCP reservation |

The single most useful command is `wifi status` over USB, which shows whether
the board thinks it is associated, what address it has, who it is streaming
to, and how many datagrams it has sent and failed to send.
