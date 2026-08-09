/*
 * bbda_imu.ino - ESP32-S3 firmware for a TDK ICM-45605 + QST QMC6309 board.
 *
 * Board wiring (fixed by the custom PCB):
 *   ICM-45605  INT1 -> GPIO17
 *   ICM-45605  SDA  -> GPIO8    (shared I2C bus, 7-bit address 0x68)
 *   ICM-45605  SCL  -> GPIO9
 *   QMC6309    SDA  -> GPIO8    (same bus, 7-bit address 0x7C)
 *   QMC6309    SCL  -> GPIO9
 *
 * The ICM-45605 is driven through TDK's own Arduino library ("ICM45605",
 * available in the Library Manager) so every APEX algorithm is the vendor
 * implementation. The QMC6309 has no vendor Arduino library, so it is
 * driven by the register-level driver in qmc6309.cpp.
 *
 * Protocol: line oriented ASCII. See protocol.h. It is carried over USB CDC
 * at 921600 baud and, once WiFi credentials are set, over UDP as well -- the
 * same bytes down both, so a serial monitor and the dashboard can watch the
 * same board at the same time. Type `help` for the command list; commands are
 * accepted from either transport.
 */

#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <stdarg.h>

#include "ICM45605.h"
#include "protocol.h"
#include "qmc6309.h"

/* Output sink, defined under "Output transports" below. Declared here because
 * the IMU subclass and the bring-up code both print, and both come first. */
static void outPrint(const char *text);
static void outPrintln(const char *text);
static void outPrintln(const __FlashStringHelper *text);
static void outPrintln();
static void outPrintf(const char *fmt, ...);
static void outFlush();
/* Both are defined further down but are needed by the network code, which has
 * to sit above them because the sample loop calls into it. */
static void emitInfo(const char *key, const char *value);
static void handleCommand(char *line);

/* Rescue hatch. `wifi auto on` makes the board join at boot -- and if joining
 * is what resets it (a supply that cannot meet the radio's transmit current is
 * the usual reason), the board reboots before it has been up long enough to
 * accept the command that would turn the setting off. The serial port then
 * appears and vanishes on a one-second cycle and there is no way in.
 *
 * Building with -DBBDA_SKIP_AUTOJOIN=1 produces a firmware that ignores the
 * stored setting, which is long enough to send `wifi auto off` and flash the
 * normal build back. It is a build-time flag precisely because the runtime is
 * the thing that is unreachable, and it leaves NVS -- calibration included --
 * untouched, which erasing the partition would not. */
#ifndef BBDA_SKIP_AUTOJOIN
#define BBDA_SKIP_AUTOJOIN 0
#endif

/* ------------------------------------------------------------------ */
/* Board configuration                                                 */
/* ------------------------------------------------------------------ */
static const int PIN_SDA      = 8;
static const int PIN_SCL      = 9;
static const int PIN_IMU_INT1 = 17;

static const uint32_t I2C_HZ    = 400000;
static const uint32_t SERIAL_BAUD = 921600;

/* UDP transport. The board listens on this port for commands and, unless a
 * destination has been pinned with `udp host`, streams back to whoever last
 * sent it one. That means the host needs no configuration on the board and a
 * board left streaming at a machine that has gone away redirects itself as
 * soon as another one speaks to it. */
static const uint16_t UDP_LISTEN_PORT_DEFAULT = 3333;

/* Lines are coalesced into datagrams rather than sent one per line: at 500 Hz
 * a datagram per sample is 500 packets a second of mostly header. The buffer
 * is flushed when it fills or when the oldest line in it reaches
 * UDP_MAX_LATENCY_US, so batching never costs more delay than that. 1200
 * bytes keeps a datagram inside a 1500-byte Ethernet MTU with room to spare,
 * so nothing is ever fragmented. */
static const size_t   UDP_BUF_SIZE      = 1200;
static const uint32_t UDP_MAX_LATENCY_US = 10000;   /* 10 ms */

/* ICM-45605 is device family "A1": max +/-16 g and +/-2000 dps
 * (INV_IMU_HIGH_FSR_SUPPORTED is 0 for this part). */
static const uint16_t ACCEL_FSR_MAX_G   = 16;
static const uint16_t GYRO_FSR_MAX_DPS  = 2000;

/* ------------------------------------------------------------------ */
/* Devices                                                             */
/* ------------------------------------------------------------------ */
/* Two driver-level calls the vendor C++ class needs but does not expose; its
 * driver handle is protected, so a subclass is the least invasive way to
 * reach them without patching a library that lives outside this repository.
 *
 * edmpDisable() exists because of a documented ordering trap. The header for
 * inv_imu_edmp_init_apex() states it returns "negative value on error or if
 * EDMP is enabled" -- but the vendor's startAPEX() calls it at the top,
 * before its own disable sequence, and leaves the engine enabled when it
 * finishes. So startAPEX() succeeds exactly once per power-on and returns -1
 * for the rest of the session, silently leaving every APEX algorithm off.
 * Disabling the engine first restores the documented precondition. */
class ICM456xxBoard : public ICM456xx {
 public:
  using ICM456xx::ICM456xx;
  int edmpDisable() { return inv_imu_edmp_disable(&icm_driver); }
  int softReset() { return inv_imu_soft_reset(&icm_driver); }
  uint8_t edmpEnableBits() {
    uint8_t v = 0xFF;
    inv_imu_read_reg(&icm_driver, EDMP_APEX_EN1, 1, &v);
    return v;
  }

  /* The UI low-pass filters. The vendor's Arduino wrapper never touches
   * these, so whatever the part resets to is what the whole project has been
   * running on -- and since a filter's job is to trade delay for smoothness,
   * "whatever it happened to be" is not a defensible setting for an input
   * device whose two complaints are delay and smoothness. Exposed so the
   * choice is made here, deliberately, and can be measured. */
  int setGyroBandwidth(uint8_t sel) {
    return inv_imu_set_gyro_ln_bw(
        &icm_driver, (ipreg_sys1_reg_172_gyro_ui_lpfbw_sel_t)sel);
  }
  int setAccelBandwidth(uint8_t sel) {
    return inv_imu_set_accel_ln_bw(
        &icm_driver, (ipreg_sys2_reg_131_accel_ui_lpfbw_t)sel);
  }
  /* Read them back, so the banner reports what the part is really doing
   * rather than what this firmware last asked for. */
  uint8_t gyroBandwidth() {
    ipreg_sys1_reg_172_t r;
    if (inv_imu_read_reg(&icm_driver, IPREG_SYS1_REG_172, 1, (uint8_t *)&r) != 0)
      return 0xFF;
    return (uint8_t)r.gyro_ui_lpfbw_sel;
  }
  uint8_t accelBandwidth() {
    ipreg_sys2_reg_131_t r;
    if (inv_imu_read_reg(&icm_driver, IPREG_SYS2_REG_131, 1, (uint8_t *)&r) != 0)
      return 0xFF;
    return (uint8_t)r.accel_ui_lpfbw_sel;
  }

  /* Reports the return code of each step the vendor's startAPEX() folds into
   * a single OR-ed result, so a failure can be attributed instead of guessed
   * at. Exposed through the `apexprobe` command. */
  void apexProbe() {
    uint8_t v = 0;
    int rc = inv_imu_read_reg(&icm_driver, EDMP_APEX_EN1, 1, &v);
    outPrintf("  EDMP_APEX_EN1        rc=%d value=0x%02X\n", rc, v);
    outPrintf("  edmp_disable         rc=%d\n", inv_imu_edmp_disable(&icm_driver));
    inv_imu_read_reg(&icm_driver, EDMP_APEX_EN1, 1, &v);
    outPrintf("  EDMP_APEX_EN1 after  value=0x%02X\n", v);
    outPrintf("  wait_for_idle        rc=%d\n", inv_imu_edmp_wait_for_idle(&icm_driver));
    outPrintf("  recompute_decimation rc=%d\n",
                  inv_imu_edmp_recompute_apex_decimation(&icm_driver));
    outPrintf("  init_apex            rc=%d\n", inv_imu_edmp_init_apex(&icm_driver));
    outFlush();
  }

};

ICM456xxBoard IMU(Wire, /*address_lsb=*/0, I2C_HZ);  /* AP_AD0 tied low -> 0x68 */
QMC6309  Compass(Wire);

/* ------------------------------------------------------------------ */
/* Runtime state                                                       */
/* ------------------------------------------------------------------ */
enum OutputMode : uint8_t { OUT_PRETTY, OUT_CSV, OUT_OFF };
enum RunMode : uint8_t { RUN_STREAM, RUN_FIFO, RUN_WOM };

static OutputMode g_out_mode = OUT_PRETTY;
static RunMode    g_run_mode = RUN_STREAM;

static uint16_t g_out_hz     = 5;      /* print rate, mode dependent  */
static bool     g_csv_calibrated = false; /* CSV emits raw by default */

/* Sensor configuration mirrors, kept so the banner can report them.
 *
 * 800 Hz rather than the 200 this used to run the gyroscope at, and it is not
 * about wanting 800 samples a second on the wire -- the stream still leaves at
 * whatever `rate` says. It is about how old a sample is when it is read.
 *
 * Stream mode polls the data registers on a timer of its own, which is not
 * synchronised to the sensor's conversions in any way. At a 200 Hz ODR the
 * register holds a value between 0 and 5 ms old, uniformly, and the timestamp
 * put on it says "now" -- so every sample carries up to 5 ms of timing error
 * that nothing downstream can see or remove, and consecutive samples carry
 * *different* amounts of it. At 800 Hz that window is 1.25 ms. The gyroscope
 * is what times a flick, so this is the cheapest millisecond in the project.
 *
 * It also matches what the accelerometer was already doing: startAPEX() forces
 * the accelerometer to 800 Hz whenever tap, free-fall, low-g or high-g are on,
 * which is the default, so the 200 in this table has been a fiction on the
 * accelerometer side for as long as APEX has been enabled. */
static uint16_t g_accel_odr = 800;
static uint16_t g_accel_fsr = 16;
static uint16_t g_gyro_odr  = 800;
static uint16_t g_gyro_fsr  = 2000;

/* UI low-pass filter selection, as the register's own encoding: 0 is the
 * filter bypassed and 1..6 divide the ODR by 4, 8, 16, 32, 64 and 128.
 *
 * ODR/8 -- 100 Hz at the 800 Hz ODR above -- is chosen rather than either
 * extreme. Bypassing the filter entirely costs nothing in delay but leaves the
 * output band open all the way up, and the stream is decimated to `rate` by a
 * plain poll with no filter of its own, so anything above half of that folds
 * straight back down into the band a flick is measured in. Filtering hard
 * instead is the classic mistake for an input device: every millisecond of
 * group delay is a millisecond the player feels, and a hand-thrown flick has
 * essentially no content above 20 Hz to be smoothed anyway.
 *
 * 100 Hz sits an octave above the fastest thing a wrist does and comfortably
 * below the 200 Hz Nyquist of the default output rate, which is the whole of
 * what a filter here is for. */
static uint8_t g_gyro_bw  = 2;   /* DIV_8 */
static uint8_t g_accel_bw = 2;   /* DIV_8 */

/* Magnetometer polls per output sample. The compass plays no part in flick
 * detection -- it is there for heading in the dashboard -- and reading it costs
 * a further I2C transaction inside the sample tick, which is time the gyro read
 * is not happening and jitter on the timestamp that follows it. Read one tick
 * in eight, so at the default rate it still lands near its own 100 Hz ODR. */
static uint16_t g_mag_divider = 8;

/* Which APEX algorithms are requested. The ICM-45605 (family A1)
 * implements exactly these; bring-to-see and activity/inactivity
 * detection exist only on the B1/C1 parts and are therefore absent. */
struct ApexConfig {
  bool tilt      = true;
  bool pedometer = true;
  bool tap       = true;
  bool r2w       = true;   /* raise to wake / sleep */
  bool freefall  = true;
  bool lowg      = true;
  bool highg     = true;
};
static ApexConfig g_apex;

static volatile bool g_irq_flag = false;
static uint32_t      g_irq_count = 0;

static bool g_imu_ok = false;
static bool g_mag_ok = false;
static bool g_configured_once = false;
/* Whether the APEX engine is actually running right now. */
static bool g_apex_active = false;

/* Frames buffered before INT1 fires in FIFO mode. */
static const uint8_t FIFO_WATERMARK = 16;

/* Latest samples, in engineering units. */
static float g_accel_g[3]  = {0, 0, 0};
static float g_gyro_dps[3] = {0, 0, 0};
static float g_mag_ut[3]   = {0, 0, 0};
static float g_temp_c      = 0;
static bool  g_mag_fresh   = false;

/* Pedometer accumulators (the APEX event only fires on change). */
static uint32_t g_step_count   = 0;
static float    g_step_cadence = 0;
static const char *g_activity  = "Unknown";

/* Stored settings.
 *
 * These sit above the first function in the file on purpose. The .ino
 * preprocessor generates prototypes for every function and inserts them
 * immediately before the first one it finds, so any type used in a signature
 * has to be declared above that point or the generated prototype will not
 * compile. */
static const uint32_t CAL_MAGIC = 0x42424441; /* "BBDA" */

struct Calibration {
  uint32_t magic;
  float gyro_bias[3];   /* dps, subtracted from the raw reading      */
  float accel_bias[3];  /* g,   subtracted before scaling            */
  float accel_scale[3]; /* per-axis gain correction, nominally 1.0   */
  float mag_bias[3];    /* uT,  hard-iron offset                     */
  float mag_soft[9];    /* 3x3 soft-iron matrix, row major, I by default */
};

static Calibration g_cal;
static Preferences g_nvs;   /* namespace "bbda", shared by calibration and network */

/* ------------------------------------------------------------------ */
/* Output transports                                                   */
/* ------------------------------------------------------------------ */
/* Every line the firmware produces goes through outPrintf()/outPrint(), which
 * fans it out to whichever transports are enabled. Nothing calls Serial
 * directly any more, so "also send it over the network" needed no changes to
 * any of the code that produces output -- and a future transport needs none
 * either. */
enum SinkBits : uint8_t { SINK_SERIAL = 1 << 0, SINK_UDP = 1 << 1 };
static uint8_t g_sinks = SINK_SERIAL | SINK_UDP;

static WiFiUDP g_udp;
static bool     g_wifi_auto     = false;   /* connect at boot */
static bool     g_wifi_started  = false;   /* WiFi.begin() has been called */
static bool     g_udp_listening = false;
static uint16_t g_udp_listen_port = UDP_LISTEN_PORT_DEFAULT;
static char     g_ssid[33] = "";
static char     g_pass[65] = "";

/* Where data goes. `pinned` means an explicit `udp host`; otherwise the
 * destination is learnt from the last command received. */
static IPAddress g_udp_peer;
static uint16_t  g_udp_peer_port = 0;
static bool      g_udp_peer_valid  = false;
static bool      g_udp_peer_pinned = false;

static char     g_udp_buf[UDP_BUF_SIZE];
static size_t   g_udp_len = 0;
static uint32_t g_udp_oldest_us = 0;
static uint32_t g_udp_packets = 0;
static uint32_t g_udp_drops = 0;

static bool udpReady() {
  return g_udp_listening && g_udp_peer_valid && (g_sinks & SINK_UDP) &&
         WiFi.status() == WL_CONNECTED;
}

static void udpFlush() {
  if (g_udp_len == 0) return;
  if (!udpReady()) { g_udp_len = 0; return; }
  if (g_udp.beginPacket(g_udp_peer, g_udp_peer_port) == 1) {
    g_udp.write((const uint8_t *)g_udp_buf, g_udp_len);
    if (g_udp.endPacket() == 1) g_udp_packets++;
    else g_udp_drops++;
  } else {
    g_udp_drops++;
  }
  g_udp_len = 0;
}

/* Appends to the datagram being built, flushing whenever it fills. Data
 * longer than the whole buffer is split across packets rather than truncated,
 * because the help text and the banner are both longer than one. */
static void udpAppend(const char *data, size_t n) {
  if (!udpReady()) return;
  while (n > 0) {
    if (g_udp_len == 0) g_udp_oldest_us = micros();
    size_t space = UDP_BUF_SIZE - g_udp_len;
    size_t take = n < space ? n : space;
    memcpy(g_udp_buf + g_udp_len, data, take);
    g_udp_len += take;
    data += take;
    n -= take;
    if (g_udp_len >= UDP_BUF_SIZE) udpFlush();
  }
}

/* Called from loop() so a slow trickle of lines is not held back waiting for
 * the buffer to fill. */
static void udpService() {
  if (g_udp_len > 0 && (uint32_t)(micros() - g_udp_oldest_us) >= UDP_MAX_LATENCY_US) {
    udpFlush();
  }
}

static void outWrite(const char *text, size_t n) {
  if (g_sinks & SINK_SERIAL) Serial.write((const uint8_t *)text, n);
  udpAppend(text, n);
}

static void outPrint(const char *text) { outWrite(text, strlen(text)); }

static void outPrintln(const char *text) {
  outPrint(text);
  outWrite("\n", 1);
}

/* Flash strings on the ESP32 are ordinary memory-mapped pointers, so the F()
 * macro this firmware already used everywhere costs nothing to keep. */
static void outPrintln(const __FlashStringHelper *text) {
  outPrintln(reinterpret_cast<const char *>(text));
}

static void outPrintln() { outWrite("\n", 1); }

/* Every formatted line in this firmware is one short line, so a 256-byte
 * frame is generous. Anything longer is passed to outPrintln() as a literal
 * instead, which has no such limit. */
static void outPrintf(const char *fmt, ...) {
  char buf[256];
  va_list args;
  va_start(args, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  if (n < 0) return;
  outWrite(buf, (size_t)n < sizeof(buf) - 1 ? (size_t)n : sizeof(buf) - 1);
}

/* Command replies are worth a millisecond of latency to have arrive at once
 * rather than trailing the next data packet. */
static void outFlush() {
  if (g_sinks & SINK_SERIAL) Serial.flush();
  udpFlush();
}

/* ------------------------------------------------------------------ */
/* Calibration and network storage                                     */
/* ------------------------------------------------------------------ */
/* The APEX selection has to survive the reboot that applies it. */
static uint8_t apexBits() {
  return (uint8_t)((g_apex.tilt << 0) | (g_apex.pedometer << 1) | (g_apex.tap << 2) |
                   (g_apex.r2w << 3) | (g_apex.freefall << 4) | (g_apex.lowg << 5) |
                   (g_apex.highg << 6));
}

static void apexFromBits(uint8_t bits) {
  g_apex.tilt      = bits & (1 << 0);
  g_apex.pedometer = bits & (1 << 1);
  g_apex.tap       = bits & (1 << 2);
  g_apex.r2w       = bits & (1 << 3);
  g_apex.freefall  = bits & (1 << 4);
  g_apex.lowg      = bits & (1 << 5);
  g_apex.highg     = bits & (1 << 6);
}


static void calSetDefaults(Calibration &c) {
  c.magic = CAL_MAGIC;
  for (int i = 0; i < 3; i++) {
    c.gyro_bias[i]   = 0.0f;
    c.accel_bias[i]  = 0.0f;
    c.accel_scale[i] = 1.0f;
    c.mag_bias[i]    = 0.0f;
  }
  for (int i = 0; i < 9; i++) c.mag_soft[i] = (i % 4 == 0) ? 1.0f : 0.0f;
}

static bool calLoad() {
  g_nvs.begin("bbda", true);
  size_t n = g_nvs.getBytesLength("cal");
  bool ok = false;
  if (n == sizeof(Calibration)) {
    Calibration tmp;
    g_nvs.getBytes("cal", &tmp, sizeof(tmp));
    if (tmp.magic == CAL_MAGIC) {
      g_cal = tmp;
      ok = true;
    }
  }
  g_nvs.end();
  if (!ok) calSetDefaults(g_cal);
  return ok;
}

static bool calSave() {
  g_nvs.begin("bbda", false);
  size_t written = g_nvs.putBytes("cal", &g_cal, sizeof(g_cal));
  g_nvs.end();
  return written == sizeof(g_cal);
}

static void applyCalibration(const float araw[3], const float graw[3], const float mraw[3],
                             float aout[3], float gout[3], float mout[3]) {
  for (int i = 0; i < 3; i++) {
    gout[i] = graw[i] - g_cal.gyro_bias[i];
    aout[i] = (araw[i] - g_cal.accel_bias[i]) * g_cal.accel_scale[i];
  }
  float mc[3];
  for (int i = 0; i < 3; i++) mc[i] = mraw[i] - g_cal.mag_bias[i];
  for (int i = 0; i < 3; i++) {
    mout[i] = g_cal.mag_soft[i * 3 + 0] * mc[0] +
              g_cal.mag_soft[i * 3 + 1] * mc[1] +
              g_cal.mag_soft[i * 3 + 2] * mc[2];
  }
}

/* ------------------------------------------------------------------ */
/* Network                                                             */
/* ------------------------------------------------------------------ */
static void netLoad() {
  g_nvs.begin("bbda", true);
  String ssid = g_nvs.getString("ssid", "");
  String pass = g_nvs.getString("pass", "");
  String host = g_nvs.getString("udphost", "");
  g_wifi_auto       = g_nvs.getBool("wifiauto", false);
  g_udp_listen_port = g_nvs.getUShort("udpport", UDP_LISTEN_PORT_DEFAULT);
  uint16_t host_port = g_nvs.getUShort("udphostp", UDP_LISTEN_PORT_DEFAULT);
  g_nvs.end();

  snprintf(g_ssid, sizeof(g_ssid), "%s", ssid.c_str());
  snprintf(g_pass, sizeof(g_pass), "%s", pass.c_str());
  if (host.length() > 0 && g_udp_peer.fromString(host)) {
    g_udp_peer_port   = host_port;
    g_udp_peer_valid  = true;
    g_udp_peer_pinned = true;
  }
}

static bool netSave() {
  g_nvs.begin("bbda", false);
  g_nvs.putString("ssid", g_ssid);
  g_nvs.putString("pass", g_pass);
  g_nvs.putBool("wifiauto", g_wifi_auto);
  g_nvs.putUShort("udpport", g_udp_listen_port);
  if (g_udp_peer_pinned) {
    g_nvs.putString("udphost", g_udp_peer.toString());
    g_nvs.putUShort("udphostp", g_udp_peer_port);
  } else {
    g_nvs.putString("udphost", "");
  }
  g_nvs.end();
  return true;
}

static const char *wifiStatusName(wl_status_t status) {
  switch (status) {
    case WL_CONNECTED:       return "connected";
    case WL_NO_SSID_AVAIL:   return "network not found";
    case WL_CONNECT_FAILED:  return "rejected - check the password";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED:    return "disconnected";
    case WL_IDLE_STATUS:     return "idle";
    default:                 return "unknown";
  }
}

static void udpStart() {
  if (g_udp_listening) return;
  if (g_udp.begin(g_udp_listen_port)) {
    g_udp_listening = true;
  } else {
    outPrintf("ERR udp: could not listen on port %u\n", g_udp_listen_port);
  }
}

static void udpStop() {
  udpFlush();
  if (g_udp_listening) {
    g_udp.stop();
    g_udp_listening = false;
  }
}

/* Joining a network blocks for up to `timeout_ms`, which stalls the sample
 * stream for that long. That is deliberate: it only ever happens from an
 * explicit `wifi connect` or at boot, and a caller that asked to join a
 * network wants to be told whether it worked, not to poll for it. */
static bool wifiConnect(uint32_t timeout_ms = 12000) {
  if (g_ssid[0] == '\0') {
    outPrintln(F("ERR wifi: no network set - use 'wifi ssid <name>' first"));
    return false;
  }
  outPrintf("NOTICE joining '%s', up to %lu s...\n", g_ssid,
            (unsigned long)(timeout_ms / 1000));
  outFlush();

  /* Credentials live in this firmware's own NVS namespace, so the core's
   * duplicate copy is only a second thing to get out of step. */
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  /* Modem sleep saves power by parking the radio between beacons, which adds
   * tens of milliseconds of jitter to anything streaming continuously. This
   * board is USB-powered and its whole job is the stream. */
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(g_ssid, g_pass);
  g_wifi_started = true;

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout_ms) {
    delay(100);
  }
  if (WiFi.status() != WL_CONNECTED) {
    outPrintf("ERR wifi: could not join '%s' (%s)\n", g_ssid,
              wifiStatusName(WiFi.status()));
    return false;
  }

  udpStart();
  outPrintf("OK wifi connected, ip %s, %d dBm, udp port %u\n",
            WiFi.localIP().toString().c_str(), WiFi.RSSI(), g_udp_listen_port);
  return true;
}

static void wifiStop() {
  udpStop();
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  g_wifi_started = false;
}

/* Commands arriving over the network. A datagram may hold several lines, and
 * the sender of any of them becomes the stream destination unless one has
 * been pinned -- which is what makes the dashboard's "just type the board's
 * IP" work with nothing configured on the board. */
static void pollUdpCommands() {
  if (!g_udp_listening) return;

  for (int size = g_udp.parsePacket(); size > 0; size = g_udp.parsePacket()) {
    char buf[512];
    int n = g_udp.read(buf, sizeof(buf) - 1);
    if (n < 0) n = 0;
    buf[n] = '\0';

    if (!g_udp_peer_pinned) {
      g_udp_peer       = g_udp.remoteIP();
      g_udp_peer_port  = g_udp.remotePort();
      g_udp_peer_valid = true;
    }

    /* strtok_r, not strtok: handleCommand() tokenises with strtok and would
     * otherwise destroy the position this loop is holding. */
    char *save = nullptr;
    for (char *line = strtok_r(buf, "\r\n", &save); line != nullptr;
         line = strtok_r(nullptr, "\r\n", &save)) {
      if (*line) handleCommand(line);
    }
    outFlush();
  }
}

static void printNetwork() {
  char buf[96];
  const bool connected = WiFi.status() == WL_CONNECTED;

  outPrintln(F("\n-- Network --"));
  emitInfo("net.ssid", g_ssid[0] ? g_ssid : "(none set)");
  if (connected) {
    snprintf(buf, sizeof(buf), "%s  (%d dBm)", WiFi.localIP().toString().c_str(),
             WiFi.RSSI());
    emitInfo("net.ip", buf);
  } else {
    emitInfo("net.ip", g_wifi_started ? wifiStatusName(WiFi.status())
                                      : "wifi off");
  }
  emitInfo("net.auto", g_wifi_auto ? "join at boot" : "off");

  snprintf(buf, sizeof(buf), "%u%s", g_udp_listen_port,
           g_udp_listening ? "" : " (not listening)");
  emitInfo("udp.listen", buf);
  if (g_udp_peer_valid) {
    snprintf(buf, sizeof(buf), "%s:%u  (%s)", g_udp_peer.toString().c_str(),
             g_udp_peer_port, g_udp_peer_pinned ? "pinned" : "last sender");
    emitInfo("udp.target", buf);
  } else {
    emitInfo("udp.target", "none yet - send any command from the host");
  }
  snprintf(buf, sizeof(buf), "%lu sent, %lu failed",
           (unsigned long)g_udp_packets, (unsigned long)g_udp_drops);
  emitInfo("udp.packets", buf);
  emitInfo("out.sinks", (g_sinks & SINK_SERIAL) && (g_sinks & SINK_UDP) ? "serial + udp"
                        : (g_sinks & SINK_SERIAL) ? "serial only"
                        : (g_sinks & SINK_UDP) ? "udp only" : "none");
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/* The library takes the FSR as a plain integer, but three of the gyro
 * settings are fractional. This returns the true full-scale value used
 * for the LSB conversion. */
static float gyroFsrExact(uint16_t coded) {
  switch (coded) {
    case 15: return 15.625f;
    case 31: return 31.25f;
    case 62: return 62.5f;
    default: return (float)coded;
  }
}

static void IRAM_ATTR imuIrqHandler() {
  g_irq_flag = true;
  g_irq_count++;
}

static float norm3(const float v[3]) {
  return sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
}

/* ------------------------------------------------------------------ */
/* Event emission                                                      */
/* ------------------------------------------------------------------ */
static void emitEvent(const char *name, const char *detail) {
  if (g_out_mode == OUT_OFF) return;
  if (g_out_mode == OUT_CSV) {
    outPrintf("E,%lu,%s,%s\n", (unsigned long)micros(), name,
                  detail ? detail : "");
  } else {
    outPrintf("  * APEX %-10s %s\n", name, detail ? detail : "");
  }
}

static void emitInfo(const char *key, const char *value) {
  if (g_out_mode == OUT_CSV) {
    outPrintf("I,%s,%s\n", key, value);
  } else {
    outPrintf("  %-22s %s\n", key, value);
  }
}

/* ------------------------------------------------------------------ */
/* Device bring-up                                                     */
/* ------------------------------------------------------------------ */

/* Vendor-library calls during mode changes are the one place this firmware
 * can get stuck, so each one announces itself before it runs and reports its
 * own return code. With `debug on` the last line printed is the call that
 * hung; without it, only failures are reported. */
static bool g_verbose_init = false;

#define RCSTEP(label, expr)                                   \
  do {                                                        \
    if (g_verbose_init) {                                     \
      outPrintf("  .. %s\n", label);                      \
      outFlush();                                         \
    }                                                         \
    int _rc = (expr);                                         \
    if (_rc != 0) {                                           \
      outPrintf("ERR step %s returned %d\n", label, _rc); \
      outFlush();                                         \
    }                                                         \
    rc |= _rc;                                                \
  } while (0)

static int startApexFeatures() {
  int rc = 0;

  /* Each start* function is called WITHOUT a handler on purpose.
   *
   * Passing a handler makes the vendor function configure the interrupt and
   * run startAPEX() there and then. Do that for all seven algorithms and
   * startAPEX() runs seven times over; it only succeeds on a freshly
   * initialised engine, so every call after the first returns -1 and the
   * later algorithms never get enabled. With no handler the call just records
   * that the algorithm is wanted, which is how TDK's own APEX_A_Events
   * example drives it.
   *
   * Order matters: setApexInterrupt() reads those flags to decide which EDMP
   * sources to unmask, so it must run after the start* calls and before the
   * single startAPEX(). */
  if (g_apex.tilt)      RCSTEP("startTiltDetection", IMU.startTiltDetection());
  if (g_apex.pedometer) RCSTEP("startPedometer",     IMU.startPedometer());
  if (g_apex.tap)       RCSTEP("startTap",           IMU.startTap());
  if (g_apex.r2w)       RCSTEP("startRaiseToWake",   IMU.startRaiseToWake());
  if (g_apex.freefall)  RCSTEP("startFreeFall",      IMU.startFreeFall());
  if (g_apex.lowg)      RCSTEP("startLowG",          IMU.startLowG());
  if (g_apex.highg)     RCSTEP("startHighG",         IMU.startHighG());

  RCSTEP("setApexInterrupt", IMU.setApexInterrupt(PIN_IMU_INT1, imuIrqHandler));

  /* Clear the EDMP enable bit immediately before startAPEX(), not earlier.
   * startAPEX() opens with inv_imu_edmp_init_apex(), which refuses to run
   * while the engine is enabled -- and a previous startAPEX() leaves it
   * enabled, because that is how it finishes. Doing this any earlier in the
   * sequence is not enough; the calls in between put the bit back. */
  if (g_verbose_init) {
    outPrintf("  .. EDMP_APEX_EN1 before disable = 0x%02X\n", IMU.edmpEnableBits());
  }
  RCSTEP("edmp disable", IMU.edmpDisable());
  if (g_verbose_init) {
    outPrintf("  .. EDMP_APEX_EN1 after  disable = 0x%02X\n", IMU.edmpEnableBits());
  }

  RCSTEP("startAPEX", IMU.startAPEX());
  return rc;
}

/* Bring the IMU into the requested run mode. Each mode owns INT1
 * exclusively, which is why they are mutually exclusive:
 *   STREAM - EDMP/APEX events on INT1, sample data read from registers
 *   FIFO   - FIFO watermark on INT1, samples drained from the FIFO
 *   WOM    - wake-on-motion on INT1, accelerometer in low-power mode
 */
/* Push the chosen UI bandwidths into the part.
 *
 * After the sensors are started, never before: startAccel()/startGyro() write
 * the ODR and mode registers, and on this part the filter selection is only
 * meaningful once the sensor it belongs to is running in low-noise mode.
 * Failure is reported and survivable -- a wrong filter is a slightly worse
 * flick, not a dead stream. */
static void applyBandwidth() {
  int rc = IMU.setGyroBandwidth(g_gyro_bw) | IMU.setAccelBandwidth(g_accel_bw);
  if (rc != 0) {
    outPrintf("NOTICE could not set the UI filter bandwidths (rc=%d); the "
              "part keeps whatever it had\n", rc);
  }
}

static int applyRunMode() {
  int rc = 0;
  detachInterrupt(digitalPinToInterrupt(PIN_IMU_INT1));
  g_irq_flag = false;

  /* Re-initialise the device before every mode change.
   *
   * The vendor library is written on the assumption that a sketch picks one
   * mode at startup and stays there -- every one of its examples does exactly
   * that. Switching live leaves the device part-configured for the old mode,
   * and going FIFO -> stream that way wedges the driver inside an I2C
   * transaction that never returns, taking the whole sketch with it. A clean
   * begin() costs a few milliseconds and makes every transition deterministic
   * regardless of what the previous mode left behind. */
  if (g_configured_once) {
    /* FIFO and wake-on-motion reconfigure cleanly in place; only the APEX
     * path does not, and re-entering stream mode is routed through a reboot
     * before it ever reaches here (see handleRunCommand). The re-init is
     * still needed because a FIFO -> anything change otherwise wedges the
     * driver inside an I2C transaction that never returns. */
    RCSTEP("edmp disable", IMU.edmpDisable());
    RCSTEP("stopAccel", IMU.stopAccel());
    RCSTEP("stopGyro", IMU.stopGyro());
    delay(30);
    RCSTEP("re-init IMU", IMU.begin());
    delay(30);
  }
  g_configured_once = true;


  switch (g_run_mode) {
    case RUN_STREAM: {
      /* APEX is best-effort. Its failure mode is documented above and costs
       * only the motion algorithms -- accelerometer, gyroscope, temperature
       * and magnetometer are all read straight from registers and do not
       * depend on it, so a failure here must not take the stream down. */
      g_apex_active = (startApexFeatures() == 0);
      if (!g_apex_active) {
        outPrintln(F("NOTICE APEX unavailable this session (needs an IMU "
                         "power cycle); sensor streaming continues"));
      }
      /* startAPEX() has already forced the accelerometer ODR to whatever
       * the slowest enabled algorithm needs (800 Hz when free-fall,
       * low-g, high-g or tap are on). Re-asserting the streaming ODR
       * here would fight that, so the accelerometer is only started when
       * no APEX algorithm claimed it. */
      /* startAPEX() picks the accelerometer ODR when it succeeds, so setting
       * it here as well would fight that. When APEX is off or failed, nothing
       * else has claimed the accelerometer and it must be started here. */
      if (!g_apex_active) {
        RCSTEP("startAccel", IMU.startAccel(g_accel_odr, g_accel_fsr));
      }
      RCSTEP("startGyro", IMU.startGyro(g_gyro_odr, g_gyro_fsr));
      applyBandwidth();
      break;
    }

    case RUN_FIFO:
      RCSTEP("startAccel", IMU.startAccel(g_accel_odr, g_accel_fsr));
      RCSTEP("startGyro", IMU.startGyro(g_gyro_odr, g_gyro_fsr));
      applyBandwidth();
      /* Batch frames rather than interrupting per sample. The vendor helper
       * arms this pin as LEVEL-triggered (attachInterrupt ... HIGH); on an
       * ESP32 a level-triggered pin that is still asserted re-enters the ISR
       * continuously, so a watermark of 1 at 200 Hz leaves the handler barely
       * able to keep ahead of it. The re-arm below makes it edge-triggered. */
      RCSTEP("enableFifoInterrupt",
             IMU.enableFifoInterrupt(PIN_IMU_INT1, imuIrqHandler, FIFO_WATERMARK));
      attachInterrupt(digitalPinToInterrupt(PIN_IMU_INT1), imuIrqHandler, RISING);
      break;

    case RUN_WOM:
      RCSTEP("stopGyro", IMU.stopGyro());
      RCSTEP("startWakeOnMotion", IMU.startWakeOnMotion(PIN_IMU_INT1, imuIrqHandler));
      break;
  }
  delay(50);
  return rc;
}

/* "ODR/8 = 100 Hz", or "bypassed", from the register's own encoding. Static
 * buffer because it is only ever used to build one line at a time and the two
 * call sites that use it twice each want two separate strings -- hence the
 * pair. */
static const char *bandwidthName(uint8_t sel, uint16_t odr) {
  static char text[2][32];
  static uint8_t slot = 0;
  char *out = text[slot & 1];
  slot++;
  if (sel == 0) {
    snprintf(out, sizeof(text[0]), "bypassed");
  } else if (sel <= 6) {
    const uint16_t divider = (uint16_t)(1u << (sel + 1));   /* 1->4 ... 6->128 */
    snprintf(out, sizeof(text[0]), "ODR/%u = %u Hz", divider,
             (unsigned)(odr / divider));
  } else {
    snprintf(out, sizeof(text[0]), "unreadable");
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* Banner                                                              */
/* ------------------------------------------------------------------ */
static void printBanner() {
  char buf[96];

  outPrintln();
  outPrintln(F("=========================================================="));
  outPrintln(F("  bing-bang-dream-adventure  IMU node"));
  outPrintln(F("  ICM-45605 (6-axis) + QMC6309 (3-axis magnetometer)"));
  outPrintln(F("=========================================================="));

  outPrintln(F("\n-- Bus --"));
  snprintf(buf, sizeof(buf), "SDA=GPIO%d  SCL=GPIO%d  %lu Hz", PIN_SDA, PIN_SCL,
           (unsigned long)I2C_HZ);
  emitInfo("i2c", buf);
  snprintf(buf, sizeof(buf), "INT1 = GPIO%d (push-pull, active high, pulsed)",
           PIN_IMU_INT1);
  emitInfo("imu.int", buf);

  outPrintln(F("\n-- ICM-45605 --"));
  emitInfo("imu.present", g_imu_ok ? "yes (0x68)" : "NO - check wiring");
  if (g_imu_ok) {
    snprintf(buf, sizeof(buf), "%u Hz, +/-%u g", g_accel_odr, g_accel_fsr);
    emitInfo("imu.accel", buf);
    snprintf(buf, sizeof(buf), "%u Hz, +/-%g dps", g_gyro_odr,
             (double)gyroFsrExact(g_gyro_fsr));
    emitInfo("imu.gyro", buf);
    /* Read back rather than echoed. The point of setting the filters at all is
     * that nobody knew what they were; printing this firmware's intention
     * would reproduce exactly that problem one level up. */
    snprintf(buf, sizeof(buf), "gyro %s, accel %s  (asked for %s / %s)",
             bandwidthName(IMU.gyroBandwidth(), g_gyro_odr),
             bandwidthName(IMU.accelBandwidth(), g_accel_odr),
             bandwidthName(g_gyro_bw, g_gyro_odr),
             bandwidthName(g_accel_bw, g_accel_odr));
    emitInfo("imu.filter", buf);
    emitInfo("imu.temp", "on-die, 25 C + raw/128");
    emitInfo("imu.apex.active", g_apex_active
             ? "yes"
             : "NO - needs an IMU power cycle, see README");
    snprintf(buf, sizeof(buf),
             "tilt=%d pedometer=%d tap=%d r2w=%d freefall=%d lowg=%d highg=%d",
             g_apex.tilt, g_apex.pedometer, g_apex.tap, g_apex.r2w,
             g_apex.freefall, g_apex.lowg, g_apex.highg);
    emitInfo("imu.apex", buf);
    emitInfo("imu.apex.note",
             "bring-to-see and activity/inactivity are B1/C1-family only");
    emitInfo("imu.fsr.max", "16 g / 2000 dps (this part has no high-FSR mode)");
  }

  outPrintln(F("\n-- QMC6309 --"));
  if (g_mag_ok) {
    snprintf(buf, sizeof(buf), "yes (0x7C), chip id 0x%02X", Compass.chipId());
    emitInfo("mag.present", buf);
    emitInfo("mag.mode", Compass.modeName());
    snprintf(buf, sizeof(buf), "%u Hz", Compass.odrHz());
    emitInfo("mag.odr", buf);
    snprintf(buf, sizeof(buf), "%s (%.0f LSB/Gauss, %.1f LSB/uT)", Compass.rangeName(),
             (double)Compass.sensitivityLsbPerGauss(),
             (double)Compass.sensitivityLsbPerMicroTesla());
    emitInfo("mag.range", buf);
    snprintf(buf, sizeof(buf), "OSR1=%u  OSR2=%u", Compass.osr1Ratio(), Compass.osr2Ratio());
    emitInfo("mag.filter", buf);
    emitInfo("mag.setreset", Compass.setResetName());
  } else {
    emitInfo("mag.present", "NO - check wiring");
  }

  outPrintln(F("\n-- Output --"));
  emitInfo("out.mode", g_out_mode == OUT_CSV ? "csv"
                       : g_out_mode == OUT_PRETTY ? "pretty" : "off");
  snprintf(buf, sizeof(buf), "%u Hz", g_out_hz);
  emitInfo("out.rate", buf);
  emitInfo("run.mode", g_run_mode == RUN_STREAM ? "stream"
                       : g_run_mode == RUN_FIFO ? "fifo" : "wom");
  emitInfo("csv.values", g_csv_calibrated ? "calibrated" : "raw");

  printNetwork();

  outPrintln(F("\nType 'help' for commands.\n"));
}

static void printCalibration() {
  char buf[128];
  outPrintln(F("\n-- Calibration (stored in NVS, applied to pretty output) --"));
  snprintf(buf, sizeof(buf), "%.5f %.5f %.5f dps", (double)g_cal.gyro_bias[0],
           (double)g_cal.gyro_bias[1], (double)g_cal.gyro_bias[2]);
  emitInfo("cal.gyro_bias", buf);
  snprintf(buf, sizeof(buf), "%.5f %.5f %.5f g", (double)g_cal.accel_bias[0],
           (double)g_cal.accel_bias[1], (double)g_cal.accel_bias[2]);
  emitInfo("cal.accel_bias", buf);
  snprintf(buf, sizeof(buf), "%.5f %.5f %.5f", (double)g_cal.accel_scale[0],
           (double)g_cal.accel_scale[1], (double)g_cal.accel_scale[2]);
  emitInfo("cal.accel_scale", buf);
  snprintf(buf, sizeof(buf), "%.3f %.3f %.3f uT", (double)g_cal.mag_bias[0],
           (double)g_cal.mag_bias[1], (double)g_cal.mag_bias[2]);
  emitInfo("cal.mag_bias", buf);
  snprintf(buf, sizeof(buf), "%.5f %.5f %.5f %.5f %.5f %.5f %.5f %.5f %.5f",
           (double)g_cal.mag_soft[0], (double)g_cal.mag_soft[1], (double)g_cal.mag_soft[2],
           (double)g_cal.mag_soft[3], (double)g_cal.mag_soft[4], (double)g_cal.mag_soft[5],
           (double)g_cal.mag_soft[6], (double)g_cal.mag_soft[7], (double)g_cal.mag_soft[8]);
  emitInfo("cal.mag_soft", buf);
  outPrintln();
}

static void printHelp() {
  outPrintln(F(
    "\n-- Commands --\n"
    "  help                       this list\n"
    "  info                       device banner and current configuration\n"
    "  mode pretty|csv|off        output format (csv is what the dashboard uses)\n"
    "  rate <hz>                  output rate, 1..1000\n"
    "  run stream|fifo|wom        INT1 owner: APEX events, FIFO, or wake-on-motion\n"
    "  csvcal on|off              emit calibrated values on the CSV stream\n"
    "\n"
    "  accel <odr> <fsr>          ODR 1,3,6,12,25,50,100,200,400,800,1600,3200,6400\n"
    "                             FSR 2,4,8,16 (g)\n"
    "  gyro  <odr> <fsr>          FSR 15,31,62,125,250,500,1000,2000 (dps)\n"
    "  filt gyro|accel|both <n>   UI low-pass: bypass, or ODR/4..ODR/128.\n"
    "                             Lower dividers filter harder and add delay;\n"
    "                             bypass is the fastest and the noisiest.\n"
    "  magdiv <n>                 read the compass one sample in n (it plays no\n"
    "                             part in flicks and costs time in the tick)\n"
    "  apex <feature> on|off      tilt|ped|tap|r2w|freefall|lowg|highg|all\n"
    "  ped reset                  zero the step counter\n"
    "\n"
    "  mag mode susp|norm|single|cont\n"
    "  mag odr 1|10|50|100|200    output data rate (Hz)\n"
    "  mag range 8|16|32          full scale (Gauss)\n"
    "  mag osr1 1|2|4|8           bandwidth filter ratio\n"
    "  mag osr2 1|2|4|8|16        low-pass filter depth\n"
    "  mag sr on|setonly|off      set/reset driver mode\n"
    "  mag selftest               run the built-in self test\n"
    "  mag reset                  soft reset and reapply defaults\n"
    "  mag reg <addr> [value]     read or write a raw register (hex or decimal)\n"
    "\n"
    "  cal show                   print stored calibration\n"
    "  cal gyro <x> <y> <z>       gyro bias, dps\n"
    "  cal abias <x> <y> <z>      accel bias, g\n"
    "  cal ascale <x> <y> <z>     accel per-axis gain\n"
    "  cal mbias <x> <y> <z>      magnetometer hard-iron offset, uT\n"
    "  cal msoft <m00..m22>       magnetometer soft-iron matrix, 9 values\n"
    "  cal save | cal clear       write to NVS / restore identity calibration\n"
    "\n"
    "  wifi ssid <name>           network name (rest of the line, spaces ok)\n"
    "  wifi pass <secret>         password, stored in NVS, never echoed back\n"
    "  wifi connect | disconnect  join or drop the network\n"
    "  wifi auto on|off           join automatically at boot\n"
    "  wifi status | wifi forget  show settings / erase the credentials\n"
    "  udp on|off                 include UDP in the output (serial is separate)\n"
    "  udp host <ip> [port]       pin the destination instead of auto-targeting\n"
    "  udp auto                   send to whoever last sent a command (default)\n"
    "  udp port <n>               local listen port, default 3333\n"
    "  sink serial|udp|both       which transports carry the output at all\n"));
}

/* ------------------------------------------------------------------ */
/* Sampling                                                            */
/* ------------------------------------------------------------------ */
/* Register-path read of the accelerometer, gyroscope and die temperature.
 * Skipped in FIFO mode, where samples arrive through serviceFifo(). */
static void readImuRegisters() {
  if (g_imu_ok) {
    inv_imu_sensor_data_t d;
    if (IMU.getDataFromRegisters(d) == 0) {
      const float afs = (float)g_accel_fsr;
      const float gfs = gyroFsrExact(g_gyro_fsr);
      for (int i = 0; i < 3; i++) {
        g_accel_g[i]  = (float)d.accel_data[i] * afs / 32768.0f;
        g_gyro_dps[i] = (float)d.gyro_data[i] * gfs / 32768.0f;
      }
      /* Datasheet temperature conversion for the 16-bit register value. */
      g_temp_c = 25.0f + (float)d.temp_data / 128.0f;
    }
  }
}

/* The magnetometer is on the host bus, so it is polled the same way in
 * every run mode. */
static void readMag() {
  if (g_mag_ok) {
    float m[3];
    int rc = Compass.readMicroTesla(m);
    if (rc == 0) {
      g_mag_ut[0] = m[0];
      g_mag_ut[1] = m[1];
      g_mag_ut[2] = m[2];
      g_mag_fresh = true;
    } else if (rc == 1) {
      g_mag_fresh = false;  /* no new sample this tick */
    }
  }
}

/* Drain every pending APEX event. Each getter clears its own latched
 * status bit, so they are all polled after a single interrupt. */
static void serviceApex() {
  if (g_run_mode != RUN_STREAM || !g_apex_active) return;
  char buf[96];

  if (g_apex.tilt && IMU.getTilt() == 1) {
    emitEvent("tilt", "angle exceeded 35 deg");
  }

  if (g_apex.pedometer) {
    uint32_t steps = 0;
    float    cadence = 0;
    char    *activity = nullptr;
    if (IMU.getPedometer(steps, cadence, activity) == 1) {
      g_step_count   = steps;
      g_step_cadence = cadence;
      g_activity     = activity ? activity : "Unknown";
      snprintf(buf, sizeof(buf), "steps=%lu cadence=%.2f/s activity=%s",
               (unsigned long)steps, (double)cadence, g_activity);
      emitEvent("pedometer", buf);
    }
  }

  if (g_apex.tap) {
    uint8_t count = 0, axis = 0, dir = 0;
    if (IMU.getTap(count, axis, dir) == 1) {
      static const char *axis_str[3] = {"X", "Y", "Z"};
      snprintf(buf, sizeof(buf), "count=%u axis=%s direction=%s", count,
               axis < 3 ? axis_str[axis] : "?", dir ? "-" : "+");
      emitEvent("tap", buf);
    }
  }

  if (g_apex.r2w) {
    int r = IMU.getRaiseToWake();
    if (r == 1) emitEvent("r2w", "wake");
    else if (r == 2) emitEvent("r2w", "sleep");
  }

  if (g_apex.freefall) {
    uint32_t duration_ms = 0;
    if (IMU.getFreefall(duration_ms) == 1) {
      snprintf(buf, sizeof(buf), "duration=%lu ms", (unsigned long)duration_ms);
      emitEvent("freefall", buf);
    }
  }

  if (g_apex.highg && IMU.getHighG() == 1) emitEvent("highg", "high-g threshold crossed");
  if (g_apex.lowg && IMU.getLowG() == 1)   emitEvent("lowg", "low-g threshold crossed");
}

static void serviceFifo() {
  if (g_run_mode != RUN_FIFO) return;
  /* Drain whatever the watermark interrupt announced. Frames are the
   * 16-byte accel+gyro+temp+timestamp layout because hires is off. */
  inv_imu_fifo_data_t frame;
  int drained = 0;
  while (drained < 64 && IMU.getDataFromFifo(frame) == 0) {
    const float afs = (float)g_accel_fsr;
    const float gfs = gyroFsrExact(g_gyro_fsr);
    for (int i = 0; i < 3; i++) {
      g_accel_g[i]  = (float)frame.byte_16.accel_data[i] * afs / 32768.0f;
      g_gyro_dps[i] = (float)frame.byte_16.gyro_data[i] * gfs / 32768.0f;
    }
    g_temp_c = 25.0f + (float)frame.byte_16.temp_data / 2.0f;
    drained++;
  }
}

static void serviceWom() {
  if (g_run_mode != RUN_WOM) return;
  emitEvent("wom", "motion detected");
}

/* ------------------------------------------------------------------ */
/* Output                                                              */
/* ------------------------------------------------------------------ */
static void printCsv() {
  float a[3], g[3], m[3];
  if (g_csv_calibrated) {
    applyCalibration(g_accel_g, g_gyro_dps, g_mag_ut, a, g, m);
  } else {
    for (int i = 0; i < 3; i++) { a[i] = g_accel_g[i]; g[i] = g_gyro_dps[i]; m[i] = g_mag_ut[i]; }
  }
  outPrintf("D,%lu,%.6f,%.6f,%.6f,%.4f,%.4f,%.4f,%.3f,%.3f,%.3f,%.2f,%d\n",
                (unsigned long)micros(), (double)a[0], (double)a[1], (double)a[2],
                (double)g[0], (double)g[1], (double)g[2], (double)m[0], (double)m[1],
                (double)m[2], (double)g_temp_c, g_mag_fresh ? 1 : 0);
}

static void printPretty() {
  float a[3], g[3], m[3];
  applyCalibration(g_accel_g, g_gyro_dps, g_mag_ut, a, g, m);

  outPrintf("[%10.3f s]  run=%s\n", millis() / 1000.0,
                g_run_mode == RUN_STREAM ? "stream"
                : g_run_mode == RUN_FIFO ? "fifo" : "wom");
  outPrintf("  ACC   g    X %+9.4f   Y %+9.4f   Z %+9.4f   | |a| %7.4f\n",
                (double)a[0], (double)a[1], (double)a[2], (double)norm3(a));
  outPrintf("  GYR   dps  X %+9.3f   Y %+9.3f   Z %+9.3f   | |w| %7.3f\n",
                (double)g[0], (double)g[1], (double)g[2], (double)norm3(g));
  if (g_mag_ok) {
    outPrintf("  MAG   uT   X %+9.2f   Y %+9.2f   Z %+9.2f   | |m| %7.2f%s\n",
                  (double)m[0], (double)m[1], (double)m[2], (double)norm3(m),
                  g_mag_fresh ? "" : "  (stale)");
  }
  outPrintf("  TEMP  C    %6.2f\n", (double)g_temp_c);
  if (g_apex.pedometer && g_apex_active) {
    outPrintf("  STEPS      %lu   cadence %.2f steps/s   activity %s\n",
                  (unsigned long)g_step_count, (double)g_step_cadence, g_activity);
  }
  outPrintf("  IRQ        %lu\n", (unsigned long)g_irq_count);
}

/* ------------------------------------------------------------------ */
/* Command interface                                                   */
/* ------------------------------------------------------------------ */
static bool tokenEquals(const char *tok, const char *expect) {
  return tok && strcasecmp(tok, expect) == 0;
}

static void setOutputMode(OutputMode mode) {
  g_out_mode = mode;
  /* Pick a sensible default rate for the new format; `rate` overrides. */
  if (mode == OUT_PRETTY) g_out_hz = 5;
  /* 400 rather than 100. A flick lasts 60-150 ms, and the direction it is
   * reported to have gone is now the integral of the whole stroke -- so the
   * sample count over that stroke is directly the number of readings the
   * answer is averaged over. At 100 Hz a fast flick is six samples, which is
   * not an average of anything; at 400 it is two dozen, and the sensor is
   * already producing 800 a second whether they are read or not. */
  else if (mode == OUT_CSV) g_out_hz = 400;
}

static void handleMagCommand(char *argv[], int argc) {
  if (argc < 2) { outPrintln(F("ERR mag: missing subcommand")); return; }
  const char *sub = argv[1];

  if (tokenEquals(sub, "selftest")) {
    int8_t delta[3];
    bool pass[3];
    int rc = Compass.selfTest(delta, pass);
    if (rc < 0) {
      outPrintf("ERR mag selftest failed (%d)\n", rc);
      return;
    }
    outPrintf("OK mag selftest %s  X=%d(%s) Y=%d(%s) Z=%d(%s)  [pass window -50..-1 LSB]\n",
                  rc == 0 ? "PASS" : "FAIL", delta[0], pass[0] ? "ok" : "bad",
                  delta[1], pass[1] ? "ok" : "bad", delta[2], pass[2] ? "ok" : "bad");
    return;
  }
  if (tokenEquals(sub, "reset")) {
    outPrintln(Compass.begin() == 0 ? F("OK mag reset") : F("ERR mag reset"));
    return;
  }
  if (tokenEquals(sub, "reg")) {
    if (argc < 3) { outPrintln(F("ERR mag reg: need address")); return; }
    uint8_t reg = (uint8_t)strtol(argv[2], nullptr, 0);
    if (argc >= 4) {
      uint8_t val = (uint8_t)strtol(argv[3], nullptr, 0);
      outPrintln(Compass.writeReg(reg, val) == 0 ? F("OK mag reg write") : F("ERR mag reg write"));
    } else {
      uint8_t val = 0;
      if (Compass.readReg(reg, val) == 0) outPrintf("OK mag reg 0x%02X = 0x%02X\n", reg, val);
      else outPrintln(F("ERR mag reg read"));
    }
    return;
  }

  if (argc < 3) { outPrintln(F("ERR mag: missing value")); return; }
  const char *val = argv[2];
  int rc = -1;

  if (tokenEquals(sub, "mode")) {
    if (tokenEquals(val, "susp") || tokenEquals(val, "suspend")) rc = Compass.setMode(QMC6309_MODE_SUSPEND);
    else if (tokenEquals(val, "norm") || tokenEquals(val, "normal")) rc = Compass.setMode(QMC6309_MODE_NORMAL);
    else if (tokenEquals(val, "single")) rc = Compass.setMode(QMC6309_MODE_SINGLE);
    else if (tokenEquals(val, "cont") || tokenEquals(val, "continuous")) rc = Compass.setMode(QMC6309_MODE_CONTINUOUS);
  } else if (tokenEquals(sub, "odr")) {
    int hz = atoi(val);
    if (hz == 1) rc = Compass.setOdr(QMC6309_ODR_1HZ);
    else if (hz == 10) rc = Compass.setOdr(QMC6309_ODR_10HZ);
    else if (hz == 50) rc = Compass.setOdr(QMC6309_ODR_50HZ);
    else if (hz == 100) rc = Compass.setOdr(QMC6309_ODR_100HZ);
    else if (hz == 200) rc = Compass.setOdr(QMC6309_ODR_200HZ);
  } else if (tokenEquals(sub, "range")) {
    int g = atoi(val);
    if (g == 8) rc = Compass.setRange(QMC6309_RANGE_8G);
    else if (g == 16) rc = Compass.setRange(QMC6309_RANGE_16G);
    else if (g == 32) rc = Compass.setRange(QMC6309_RANGE_32G);
  } else if (tokenEquals(sub, "osr1")) {
    int r = atoi(val);
    if (r == 1) rc = Compass.setOsr1(QMC6309_OSR1_1);
    else if (r == 2) rc = Compass.setOsr1(QMC6309_OSR1_2);
    else if (r == 4) rc = Compass.setOsr1(QMC6309_OSR1_4);
    else if (r == 8) rc = Compass.setOsr1(QMC6309_OSR1_8);
  } else if (tokenEquals(sub, "osr2")) {
    int r = atoi(val);
    if (r == 1) rc = Compass.setOsr2(QMC6309_OSR2_1);
    else if (r == 2) rc = Compass.setOsr2(QMC6309_OSR2_2);
    else if (r == 4) rc = Compass.setOsr2(QMC6309_OSR2_4);
    else if (r == 8) rc = Compass.setOsr2(QMC6309_OSR2_8);
    else if (r == 16) rc = Compass.setOsr2(QMC6309_OSR2_16);
  } else if (tokenEquals(sub, "sr")) {
    if (tokenEquals(val, "on")) rc = Compass.setSetResetMode(QMC6309_SR_SET_AND_RESET_ON);
    else if (tokenEquals(val, "setonly")) rc = Compass.setSetResetMode(QMC6309_SR_SET_ONLY_ON);
    else if (tokenEquals(val, "off")) rc = Compass.setSetResetMode(QMC6309_SR_SET_AND_RESET_OFF);
  }

  outPrintln(rc == 0 ? F("OK") : F("ERR mag: bad subcommand or value"));
}

static void handleCalCommand(char *argv[], int argc) {
  if (argc < 2) { outPrintln(F("ERR cal: missing subcommand")); return; }
  const char *sub = argv[1];

  if (tokenEquals(sub, "show")) { printCalibration(); return; }
  if (tokenEquals(sub, "save")) {
    outPrintln(calSave() ? F("OK cal saved") : F("ERR cal save failed"));
    return;
  }
  if (tokenEquals(sub, "clear")) {
    calSetDefaults(g_cal);
    calSave();
    outPrintln(F("OK cal cleared"));
    return;
  }

  float *dst = nullptr;
  int need = 3;
  if (tokenEquals(sub, "gyro")) dst = g_cal.gyro_bias;
  else if (tokenEquals(sub, "abias")) dst = g_cal.accel_bias;
  else if (tokenEquals(sub, "ascale")) dst = g_cal.accel_scale;
  else if (tokenEquals(sub, "mbias")) dst = g_cal.mag_bias;
  else if (tokenEquals(sub, "msoft")) { dst = g_cal.mag_soft; need = 9; }

  if (!dst) { outPrintln(F("ERR cal: unknown subcommand")); return; }
  if (argc < 2 + need) { outPrintf("ERR cal: need %d values\n", need); return; }

  /* The accelerometer gain is the one field here whose plausible range is
   * known in advance, and it is the one that has actually been corrupted: a
   * six-position calibration where two captures were the same face divides by
   * a near-zero span and produces a gain of twenty or more. Stored, it makes
   * the part read several g while lying still, which quietly disables
   * everything that finds vertical from gravity and shows up only as flicks
   * going the wrong way. A part whose sensitivity is out by more than a
   * quarter is a broken part, not a part needing this much trim, so refusing
   * it here costs nothing real and closes the hole at the last point before
   * NVS. */
  if (tokenEquals(sub, "ascale")) {
    for (int i = 0; i < need; i++) {
      const float value = atof(argv[2 + i]);
      if (!(value > 0.75f && value < 1.25f)) {
        outPrintf("ERR cal ascale: %g is not a believable gain -- an "
                  "accelerometer axis reads within a few percent of true, so "
                  "this came from two calibration positions that were really "
                  "the same one. Redo the six-position step.\n",
                  (double)value);
        return;
      }
    }
  }

  for (int i = 0; i < need; i++) dst[i] = atof(argv[2 + i]);
  outPrintln(F("OK"));
}

/* Selecting a run mode.
 *
 * Entering stream mode needs the APEX engine initialised, and on this part
 * that can only be done once per IMU power-on. inv_imu_edmp_init_apex() --
 * the first call inside the vendor's startAPEX() -- returns -1 for the rest
 * of the session once startAPEX() has run, and measured per call it is the
 * only step in the whole sequence that ever fails. None of these bring it
 * back: clearing the EDMP enable bit (before, after, or immediately
 * preceding the call), inv_imu_soft_reset(), inv_imu_adv_device_reset() via
 * begin(), powering both sensors down, longer settling delays, or even
 * ESP.restart() -- a CPU reset leaves the IMU powered and holding its state.
 * Only a genuine power cycle of the IMU clears it.
 *
 * So APEX is not treated as required. If it cannot be re-initialised the
 * sample stream is brought up anyway and the loss is reported, rather than
 * failing the whole mode change and leaving the board looking dead. */
static void handleRunCommand(char *argv[], int argc) {
  if (argc < 2) { outPrintln(F("ERR run: need stream|fifo|wom")); return; }

  if (tokenEquals(argv[1], "stream")) g_run_mode = RUN_STREAM;
  else if (tokenEquals(argv[1], "fifo")) g_run_mode = RUN_FIFO;
  else if (tokenEquals(argv[1], "wom")) g_run_mode = RUN_WOM;
  else { outPrintln(F("ERR run: need stream|fifo|wom")); return; }

  outPrintln(applyRunMode() == 0 ? F("OK") : F("ERR run: reconfigure failed"));
}

static void handleApexCommand(char *argv[], int argc) {
  if (argc < 3) { outPrintln(F("ERR apex: need <feature> on|off")); return; }
  const bool on = tokenEquals(argv[2], "on");
  const char *f = argv[1];
  bool matched = true;

  if (tokenEquals(f, "tilt")) g_apex.tilt = on;
  else if (tokenEquals(f, "ped")) g_apex.pedometer = on;
  else if (tokenEquals(f, "tap")) g_apex.tap = on;
  else if (tokenEquals(f, "r2w")) g_apex.r2w = on;
  else if (tokenEquals(f, "freefall")) g_apex.freefall = on;
  else if (tokenEquals(f, "lowg")) g_apex.lowg = on;
  else if (tokenEquals(f, "highg")) g_apex.highg = on;
  else if (tokenEquals(f, "all")) {
    g_apex.tilt = g_apex.pedometer = g_apex.tap = g_apex.r2w = on;
    g_apex.freefall = g_apex.lowg = g_apex.highg = on;
  } else matched = false;

  if (!matched) { outPrintln(F("ERR apex: unknown feature")); return; }

  int rc = applyRunMode();
  outPrintln(rc == 0 ? F("OK") : F("ERR apex: reconfigure failed"));
}

/* Pointer into `raw` just past `count` whitespace-separated tokens.
 *
 * Network names and passwords routinely contain spaces, so `wifi ssid` and
 * `wifi pass` take the whole rest of the line rather than one token. Every
 * other command is happy with the tokeniser. */
static const char *tailAfter(const char *raw, int count) {
  const char *p = raw;
  for (int i = 0; i < count; i++) {
    while (*p == ' ' || *p == '\t') p++;
    while (*p && *p != ' ' && *p != '\t') p++;
  }
  while (*p == ' ' || *p == '\t') p++;
  return p;
}

static void handleWifiCommand(char *argv[], int argc, const char *raw) {
  if (argc < 2) { outPrintln(F("ERR wifi: need ssid|pass|connect|disconnect|auto|status|forget")); return; }
  const char *sub = argv[1];

  if (tokenEquals(sub, "status")) {
    printNetwork();
    return;
  }
  if (tokenEquals(sub, "connect")) {
    outPrintln(wifiConnect() ? F("OK") : F("ERR wifi: not connected"));
    return;
  }
  if (tokenEquals(sub, "disconnect")) {
    wifiStop();
    outPrintln(F("OK wifi off"));
    return;
  }
  if (tokenEquals(sub, "forget")) {
    g_ssid[0] = '\0';
    g_pass[0] = '\0';
    g_wifi_auto = false;
    g_udp_peer_pinned = false;
    g_udp_peer_valid = false;
    netSave();
    wifiStop();
    outPrintln(F("OK wifi credentials cleared"));
    return;
  }
  if (tokenEquals(sub, "ssid")) {
    const char *value = tailAfter(raw, 2);
    if (*value == '\0') { outPrintln(F("ERR wifi ssid: need a network name")); return; }
    snprintf(g_ssid, sizeof(g_ssid), "%s", value);
    netSave();
    outPrintf("OK wifi ssid '%s'\n", g_ssid);
    return;
  }
  if (tokenEquals(sub, "pass")) {
    const char *value = tailAfter(raw, 2);
    snprintf(g_pass, sizeof(g_pass), "%s", value);
    netSave();
    /* Never echoed back: this line is about to go out over the very network
     * it is the password for, and into anyone's serial log. */
    outPrintf("OK wifi password set (%u characters)\n", (unsigned)strlen(g_pass));
    return;
  }
  if (tokenEquals(sub, "auto")) {
    if (argc < 3) { outPrintln(F("ERR wifi auto: need on|off")); return; }
    g_wifi_auto = tokenEquals(argv[2], "on");
    netSave();
    outPrintln(F("OK"));
    return;
  }
  outPrintln(F("ERR wifi: unknown subcommand"));
}

static void handleUdpCommand(char *argv[], int argc) {
  if (argc < 2) { outPrintln(F("ERR udp: need on|off|host|auto|port|status")); return; }
  const char *sub = argv[1];

  if (tokenEquals(sub, "status")) { printNetwork(); return; }
  if (tokenEquals(sub, "on") || tokenEquals(sub, "off")) {
    const bool on = tokenEquals(sub, "on");
    g_sinks = on ? (uint8_t)(g_sinks | SINK_UDP) : (uint8_t)(g_sinks & ~SINK_UDP);
    outPrintln(F("OK"));
    return;
  }
  if (tokenEquals(sub, "auto")) {
    g_udp_peer_pinned = false;
    netSave();
    outPrintln(F("OK udp target follows whoever last sent a command"));
    return;
  }
  if (tokenEquals(sub, "host")) {
    if (argc < 3) { outPrintln(F("ERR udp host: need an IP address")); return; }
    IPAddress address;
    if (!address.fromString(argv[2])) {
      outPrintln(F("ERR udp host: not a valid IPv4 address"));
      return;
    }
    g_udp_peer = address;
    g_udp_peer_port = (argc >= 4) ? (uint16_t)atoi(argv[3]) : UDP_LISTEN_PORT_DEFAULT;
    g_udp_peer_valid = true;
    g_udp_peer_pinned = true;
    netSave();
    outPrintf("OK udp target %s:%u\n", g_udp_peer.toString().c_str(), g_udp_peer_port);
    return;
  }
  if (tokenEquals(sub, "port")) {
    if (argc < 3) { outPrintln(F("ERR udp port: need a port number")); return; }
    int port = atoi(argv[2]);
    if (port < 1 || port > 65535) { outPrintln(F("ERR udp port: 1..65535")); return; }
    g_udp_listen_port = (uint16_t)port;
    netSave();
    if (g_udp_listening) { udpStop(); udpStart(); }
    outPrintf("OK udp listening on %u\n", g_udp_listen_port);
    return;
  }
  outPrintln(F("ERR udp: unknown subcommand"));
}

static void handleCommand(char *line) {
  /* Kept before the tokeniser chews through the line, for the two commands
   * whose argument may contain spaces. */
  char raw[192];
  snprintf(raw, sizeof(raw), "%s", line);

  char *argv[16];
  int argc = 0;
  for (char *tok = strtok(line, " \t"); tok && argc < 16; tok = strtok(nullptr, " \t")) {
    argv[argc++] = tok;
  }
  if (argc == 0) return;

  const char *cmd = argv[0];

  if (tokenEquals(cmd, "help") || tokenEquals(cmd, "?")) {
    printHelp();
  } else if (tokenEquals(cmd, "info")) {
    printBanner();
    printCalibration();
  } else if (tokenEquals(cmd, "mode")) {
    if (argc < 2) { outPrintln(F("ERR mode: need pretty|csv|off")); return; }
    if (tokenEquals(argv[1], "pretty")) setOutputMode(OUT_PRETTY);
    else if (tokenEquals(argv[1], "csv")) setOutputMode(OUT_CSV);
    else if (tokenEquals(argv[1], "off")) setOutputMode(OUT_OFF);
    else { outPrintln(F("ERR mode: need pretty|csv|off")); return; }
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "rate")) {
    if (argc < 2) { outPrintln(F("ERR rate: need hz")); return; }
    int hz = atoi(argv[1]);
    /* Raised from 500. The gyroscope runs at 800 Hz and the ceiling had no
     * reason to sit below it -- a stream slower than the sensor throws away
     * samples that were already paid for, and at 400 Hz a 120 ms flick is
     * resolved by 48 samples instead of 24. */
    if (hz < 1 || hz > 1000) { outPrintln(F("ERR rate: 1..1000")); return; }
    g_out_hz = (uint16_t)hz;
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "filt")) {
    /* filt <gyro|accel|both> <bypass|4|8|16|32|64|128> */
    if (argc < 3) {
      outPrintln(F("ERR filt: need <gyro|accel|both> <bypass|4|8|16|32|64|128>"));
      return;
    }
    uint8_t sel = 0xFF;
    if (tokenEquals(argv[2], "bypass") || tokenEquals(argv[2], "off")) {
      sel = 0;
    } else {
      const int divider = atoi(argv[2]);
      for (uint8_t i = 1; i <= 6; i++) {
        if (divider == (1 << (i + 1))) { sel = i; break; }
      }
    }
    if (sel == 0xFF) {
      outPrintln(F("ERR filt: divider must be bypass, 4, 8, 16, 32, 64 or 128"));
      return;
    }
    const bool want_gyro  = tokenEquals(argv[1], "gyro") || tokenEquals(argv[1], "both");
    const bool want_accel = tokenEquals(argv[1], "accel") || tokenEquals(argv[1], "both");
    if (!want_gyro && !want_accel) {
      outPrintln(F("ERR filt: first argument is gyro, accel or both"));
      return;
    }
    int rc = 0;
    if (want_gyro)  { g_gyro_bw  = sel; rc |= IMU.setGyroBandwidth(sel); }
    if (want_accel) { g_accel_bw = sel; rc |= IMU.setAccelBandwidth(sel); }
    if (rc != 0) { outPrintln(F("ERR filt: the part rejected it")); return; }
    outPrintf("OK gyro %s, accel %s\n",
              bandwidthName(IMU.gyroBandwidth(), g_gyro_odr),
              bandwidthName(IMU.accelBandwidth(), g_accel_odr));
  } else if (tokenEquals(cmd, "magdiv")) {
    if (argc < 2) { outPrintln(F("ERR magdiv: need a count, 1..64")); return; }
    int n = atoi(argv[1]);
    if (n < 1 || n > 64) { outPrintln(F("ERR magdiv: 1..64")); return; }
    g_mag_divider = (uint16_t)n;
    outPrintf("OK compass read once every %d samples\n", n);
  } else if (tokenEquals(cmd, "apexprobe")) {
    IMU.apexProbe();
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "debug")) {
    if (argc < 2) { outPrintln(F("ERR debug: need on|off")); return; }
    g_verbose_init = tokenEquals(argv[1], "on");
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "csvcal")) {
    if (argc < 2) { outPrintln(F("ERR csvcal: need on|off")); return; }
    g_csv_calibrated = tokenEquals(argv[1], "on");
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "run")) {
    handleRunCommand(argv, argc);
  } else if (tokenEquals(cmd, "accel")) {
    if (argc < 3) { outPrintln(F("ERR accel: need <odr> <fsr>")); return; }
    uint16_t fsr = (uint16_t)atoi(argv[2]);
    if (fsr > ACCEL_FSR_MAX_G) { outPrintln(F("ERR accel: max FSR is 16 g")); return; }
    g_accel_odr = (uint16_t)atoi(argv[1]);
    g_accel_fsr = fsr;
    outPrintln(IMU.startAccel(g_accel_odr, g_accel_fsr) == 0 ? F("OK") : F("ERR accel"));
  } else if (tokenEquals(cmd, "gyro")) {
    if (argc < 3) { outPrintln(F("ERR gyro: need <odr> <fsr>")); return; }
    uint16_t fsr = (uint16_t)atoi(argv[2]);
    if (fsr > GYRO_FSR_MAX_DPS) { outPrintln(F("ERR gyro: max FSR is 2000 dps")); return; }
    g_gyro_odr = (uint16_t)atoi(argv[1]);
    g_gyro_fsr = fsr;
    outPrintln(IMU.startGyro(g_gyro_odr, g_gyro_fsr) == 0 ? F("OK") : F("ERR gyro"));
  } else if (tokenEquals(cmd, "apex")) {
    handleApexCommand(argv, argc);
  } else if (tokenEquals(cmd, "ped")) {
    g_step_count = 0;
    g_step_cadence = 0;
    outPrintln(F("OK"));
  } else if (tokenEquals(cmd, "mag")) {
    handleMagCommand(argv, argc);
  } else if (tokenEquals(cmd, "cal")) {
    handleCalCommand(argv, argc);
  } else if (tokenEquals(cmd, "wifi")) {
    handleWifiCommand(argv, argc, raw);
  } else if (tokenEquals(cmd, "udp")) {
    handleUdpCommand(argv, argc);
  } else if (tokenEquals(cmd, "sink")) {
    if (argc < 2) { outPrintln(F("ERR sink: need serial|udp|both")); return; }
    if (tokenEquals(argv[1], "serial")) g_sinks = SINK_SERIAL;
    else if (tokenEquals(argv[1], "udp")) g_sinks = SINK_UDP;
    else if (tokenEquals(argv[1], "both")) g_sinks = SINK_SERIAL | SINK_UDP;
    else { outPrintln(F("ERR sink: need serial|udp|both")); return; }
    outPrintln(F("OK"));
  } else {
    outPrintf("ERR unknown command '%s' (try 'help')\n", cmd);
  }
}

static void pollSerialCommands() {
  static char buf[192];
  static size_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = '\0';
      if (len > 0) handleCommand(buf);
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

/* ------------------------------------------------------------------ */
/* Arduino entry points                                                */
/* ------------------------------------------------------------------ */
void setup() {
  Serial.begin(SERIAL_BAUD);

#if ARDUINO_USB_CDC_ON_BOOT
  /* Never let a write to USB wait for the host.
   *
   * With "USB CDC On Boot" enabled, `Serial` is not a UART: it is a USB CDC
   * endpoint, and its write() blocks until the host drains the buffer or a
   * timeout expires. That timeout defaults to 100 ms, and the buffer only
   * drains when something on the other end is actually reading.
   *
   * At 200 Hz that is fatal rather than merely slow. Every sample is a
   * write(); if the host stops reading -- the bridge exits, the game is
   * alt-tabbed away, a serial monitor is left open but scrolled back, the
   * cable is still plugged into a sleeping laptop -- each one stalls loop()
   * for 100 ms. The IRQ that services the FIFO stops being polled, samples
   * are lost, WiFi is starved, and the board looks like it has crashed while
   * being perfectly healthy. Over UDP the symptom is stranger still: the
   * network output stutters for a reason that lives entirely in the USB
   * stack, which is not where anyone looks.
   *
   * A timeout of zero makes write() drop what it cannot deliver and return
   * immediately. That is the right trade for telemetry: the newest sample is
   * always worth more than the one being waited on, and a reader that has
   * gone away is not owed a backlog. Records that do arrive are unaffected --
   * each line is written whole -- and every record carries a timestamp, so a
   * dropped one shows up as a gap rather than as a corrupted line.
   *
   * Guarded because with CDC on boot *disabled* `Serial` is a HardwareSerial
   * on UART0, which has no such method and would not compile. */
  Serial.setTxTimeoutMs(0);
#endif

  /* Wait a moment for a host to open the port, so the banner is not lost to
   * a terminal that was a fraction of a second late. Bounded because the
   * board must come up with no host at all: on WiFi, or on a charger. */
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) { delay(10); }

  calLoad();
  netLoad();

  /* Pick up a run mode and APEX selection left by a reboot-to-reconfigure. */
  g_nvs.begin("bbda", true);
  uint8_t stored_mode = g_nvs.getUChar("runmode", (uint8_t)RUN_STREAM);
  uint8_t stored_apex = g_nvs.getUChar("apex", apexBits());
  g_nvs.end();
  if (stored_mode <= (uint8_t)RUN_WOM) g_run_mode = (RunMode)stored_mode;
  apexFromBits(stored_apex);

  /* Claim the board's I2C pins before the IMU library calls Wire.begin()
   * with no arguments, which would otherwise use the core defaults. */
  Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
  /* A wedged transaction should surface as a failed read, not as a sketch
   * that stops responding forever. */
  Wire.setTimeOut(50);

  int rc = IMU.begin();
  g_imu_ok = (rc == 0);
  if (!g_imu_ok) {
    outPrintf("ERR ICM-45605 init failed (%d)\n", rc);
  }

  g_mag_ok = (Compass.begin() == 0);
  if (!g_mag_ok) {
    outPrintln(F("ERR QMC6309 init failed (chip ID mismatch or no ACK at 0x7C)"));
  }

  if (g_imu_ok) {
    if (applyRunMode() != 0) outPrintln(F("ERR IMU run-mode configuration failed"));
  }

  /* After the sensors, so a network that refuses to come up delays the
   * banner rather than the bring-up, and never stops the board working over
   * USB -- which is the transport you have when the network is the problem. */
  if (g_wifi_auto && g_ssid[0] != '\0' && !BBDA_SKIP_AUTOJOIN) {
    /* Joining is the one thing in setup() that can reset the board rather
     * than fail: bringing the radio up draws several hundred milliamps, and a
     * supply that cannot meet it browns out. With `wifi auto on` that reset
     * lands back here, and the board reboots roughly once a second for ever --
     * never up long enough to accept the `wifi auto off` that would stop it.
     * The serial port appears and vanishes on the same cycle, so there is no
     * way in short of reflashing.
     *
     * So the attempt is recorded before it is made and cleared once it
     * succeeds. Two boots that both died mid-join means joining is what is
     * killing the board, and the third comes up on USB with the radio off,
     * saying why. The setting is left alone -- the board is not entitled to
     * decide it was wrong, only to stop walking into it. */
    g_nvs.begin("bbda", false);
    uint8_t attempts = g_nvs.getUChar("joinfail", 0);
    if (attempts >= 2) {
      g_nvs.putUChar("joinfail", 0);
      g_nvs.end();
      outPrintln(F("ERR wifi: two boots died while joining, so the radio is "
                   "off this time."));
      outPrintln(F("    Almost always a supply that cannot meet the radio's "
                   "transmit current:"));
      outPrintln(F("    try a shorter or thicker USB cable, a different port, "
                   "or a powered hub."));
      outPrintln(F("    `wifi connect` retries now; `wifi auto off` stops it "
                   "trying at boot."));
    } else {
      g_nvs.putUChar("joinfail", (uint8_t)(attempts + 1));
      g_nvs.end();
      const bool joined = wifiConnect();
      g_nvs.begin("bbda", false);
      g_nvs.putUChar("joinfail", joined ? 0 : (uint8_t)(attempts + 1));
      g_nvs.end();
    }
  }

  printBanner();
  printCalibration();
}

void loop() {
  pollSerialCommands();
  pollUdpCommands();

  if (g_irq_flag) {
    g_irq_flag = false;
    serviceApex();
    serviceFifo();
    serviceWom();
  }

  static uint32_t next_us = 0;
  static uint16_t mag_tick = 0;
  const uint32_t period_us = 1000000UL / (g_out_hz ? g_out_hz : 1);
  uint32_t now = micros();
  if ((int32_t)(now - next_us) >= 0) {
    /* Advance the schedule from where it *should* have fired, not from now.
     * Adding the period to `now` folds however late this tick was into the
     * next one, so a tick delayed by a command or a WiFi flush pushes every
     * later tick back with it and the stream drifts slow. Anchoring to the
     * grid keeps the average rate exact; the catch-up guard is for the case
     * where the loop has fallen so far behind that chasing the grid would
     * fire a burst of back-to-back samples. */
    next_us += period_us;
    if ((int32_t)(now - next_us) >= 0) next_us = now + period_us;

    if (g_run_mode != RUN_FIFO) readImuRegisters();
    /* Read the gyro and accelerometer first and the compass afterwards. The
     * timestamp printed below is taken at print time, so whatever is read last
     * is the freshest -- and it is the gyroscope, not the compass, that has to
     * be fresh. Decimated as well, so most ticks skip the second transaction
     * entirely. */
    if (g_mag_divider <= 1 || (mag_tick % g_mag_divider) == 0) {
      readMag();
    } else {
      /* Say so rather than repeating the last reading as though it were new.
       * The freshness flag is the whole contract the host has for telling a
       * repeated magnetometer value from a measured one, and a decimator that
       * quietly left it true would break that for every reader at once. */
      g_mag_fresh = false;
    }
    mag_tick++;

    if (g_out_mode == OUT_CSV) printCsv();
    else if (g_out_mode == OUT_PRETTY) printPretty();
  }

  /* Push out a part-filled datagram once it has been waiting long enough, so
   * a slow output rate is not held hostage to the buffer filling up. */
  udpService();
}
