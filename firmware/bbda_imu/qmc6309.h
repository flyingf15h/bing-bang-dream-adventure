/*
 * qmc6309.h - QST QMC6309 3-axis magnetometer driver
 *
 * Written against: QST document 13-52-22, "QMC6309 Datasheet", Rev. C.
 * Every register and every control field described in section 9 of that
 * datasheet is exposed here.
 *
 * Device summary (datasheet section 2.1):
 *   - 3-axis AMR magnetic sensor, 16-bit ADC, WLCSP 0.8x0.8x0.5 mm
 *   - Full scale +/-32 Gauss, field resolution down to 2.5 mGauss
 *   - Built-in temperature compensation and self-test
 *   - Single fixed I2C address 0x7C (7-bit), 100/400 kHz
 */

#ifndef QMC6309_H
#define QMC6309_H

#include <Arduino.h>
#include <Wire.h>

/* ------------------------------------------------------------------ */
/* Register map (datasheet Table 12)                                   */
/* ------------------------------------------------------------------ */
#define QMC6309_I2C_ADDR      0x7C  /* 7-bit; only address available   */
#define QMC6309_CHIP_ID_VALUE 0x90  /* POR value of register 0x00      */

#define QMC6309_REG_CHIP_ID   0x00  /* R   Chip ID, reads 0x90         */
#define QMC6309_REG_XOUT_L    0x01  /* R   X LSB                       */
#define QMC6309_REG_XOUT_H    0x02  /* R   X MSB                       */
#define QMC6309_REG_YOUT_L    0x03  /* R   Y LSB                       */
#define QMC6309_REG_YOUT_H    0x04  /* R   Y MSB                       */
#define QMC6309_REG_ZOUT_L    0x05  /* R   Z LSB                       */
#define QMC6309_REG_ZOUT_H    0x06  /* R   Z MSB                       */
#define QMC6309_REG_STATUS    0x09  /* R   Status flags                */
#define QMC6309_REG_CTRL1     0x0A  /* R/W OSR2 | OSR1 | MODE          */
#define QMC6309_REG_CTRL2     0x0B  /* R/W SOFT_RST | ODR | RNG | S/R  */
#define QMC6309_REG_CTRL3     0x0E  /* R/W SELFTEST                    */
#define QMC6309_REG_ST_X      0x13  /* R   Self-test X delta (8-bit)   */
#define QMC6309_REG_ST_Y      0x14  /* R   Self-test Y delta (8-bit)   */
#define QMC6309_REG_ST_Z      0x15  /* R   Self-test Z delta (8-bit)   */

/* Status register bits (datasheet Table 14) */
#define QMC6309_STATUS_DRDY          0x01 /* new data ready            */
#define QMC6309_STATUS_OVFL          0x02 /* |data| exceeded +/-32000  */
#define QMC6309_STATUS_ST_RDY        0x04 /* self-test finished        */
#define QMC6309_STATUS_NVM_RDY       0x08 /* NVM ready for access      */
#define QMC6309_STATUS_NVM_LOAD_DONE 0x10 /* NVM load finished         */

/* ------------------------------------------------------------------ */
/* Control field encodings (datasheet Tables 15, 16, 17)               */
/* ------------------------------------------------------------------ */

/* CTRL1 bits [1:0] - operating mode. POR default is Suspend.
 * The datasheet requires passing through Suspend when switching between
 * Normal / Single / Continuous; setMode() does that automatically. */
enum QMC6309Mode : uint8_t {
  QMC6309_MODE_SUSPEND    = 0,
  QMC6309_MODE_NORMAL     = 1,
  QMC6309_MODE_SINGLE     = 2,
  QMC6309_MODE_CONTINUOUS = 3,
};

/* CTRL1 bits [4:3] - OSR1, internal digital filter bandwidth.
 * Larger ratio = narrower bandwidth, less noise, more current. */
enum QMC6309Osr1 : uint8_t {
  QMC6309_OSR1_8 = 0,
  QMC6309_OSR1_4 = 1,
  QMC6309_OSR1_2 = 2,
  QMC6309_OSR1_1 = 3,
};

/* CTRL1 bits [7:5] - OSR2, secondary low-pass filter depth. */
enum QMC6309Osr2 : uint8_t {
  QMC6309_OSR2_1  = 0,
  QMC6309_OSR2_2  = 1,
  QMC6309_OSR2_4  = 2,
  QMC6309_OSR2_8  = 3,
  QMC6309_OSR2_16 = 4,   /* encodings 4..7 all select 16 */
};

/* CTRL2 bits [3:2] - full-scale range. Lower range = higher sensitivity. */
enum QMC6309Range : uint8_t {
  QMC6309_RANGE_32G = 0, /* 1000 LSB/Gauss */
  QMC6309_RANGE_16G = 1, /* 2000 LSB/Gauss */
  QMC6309_RANGE_8G  = 2, /* 4000 LSB/Gauss */
  /* encoding 3 also selects 32 G */
};

/* CTRL2 bits [6:4] - output data rate, used in Normal mode. */
enum QMC6309Odr : uint8_t {
  QMC6309_ODR_1HZ   = 0,
  QMC6309_ODR_10HZ  = 1,
  QMC6309_ODR_50HZ  = 2,
  QMC6309_ODR_100HZ = 3,
  QMC6309_ODR_200HZ = 4,  /* encodings 4..7 all select 200 Hz */
};

/* CTRL2 bits [1:0] - set/reset driver mode. With the driver off the
 * internal offset is not renewed between measurements. */
enum QMC6309SetReset : uint8_t {
  QMC6309_SR_SET_AND_RESET_ON = 0,
  QMC6309_SR_SET_ONLY_ON      = 1,
  QMC6309_SR_SET_AND_RESET_OFF = 3,
};

/* Decoded status register */
struct QMC6309Status {
  bool data_ready;
  bool overflow;
  bool selftest_ready;
  bool nvm_ready;
  bool nvm_load_done;
  uint8_t raw;
};

class QMC6309 {
 public:
  explicit QMC6309(TwoWire &bus = Wire, uint8_t address = QMC6309_I2C_ADDR)
      : wire_(&bus), addr_(address) {}

  /* Probe the chip ID, soft-reset, and apply a default configuration:
   * Normal mode, 200 Hz, +/-8 G, OSR1=8, OSR2=8, set/reset on.
   * Returns 0 on success, negative on error. */
  int begin();

  /* Full soft reset (CTRL2.SOFT_RST). Per the datasheet the bit does not
   * self-clear, so this writes 0x80 then 0x00. Leaves the part in Suspend. */
  int softReset();

  /* Individual control fields. Each is read-modify-write so the other
   * fields in the register are preserved. */
  int setMode(QMC6309Mode mode);
  int setOsr1(QMC6309Osr1 osr);
  int setOsr2(QMC6309Osr2 osr);
  int setOdr(QMC6309Odr odr);
  int setRange(QMC6309Range range);
  int setSetResetMode(QMC6309SetReset sr);

  /* Read and decode the status register. Note that reading it clears
   * DRDY and OVFL (datasheet section 9.2.2). */
  int readStatus(QMC6309Status &out);

  /* Read registers 0x01..0x06 as raw 16-bit two's complement counts. */
  int readRaw(int16_t out[3]);

  /* Read and convert to microtesla using the currently configured range.
   * Returns 0 on success, 1 if no new sample was ready (out untouched). */
  int readMicroTesla(float out[3]);

  /* Built-in self-test (datasheet section 7.3). Switches the part to
   * Continuous mode, triggers the test, waits for ST_RDY and reads the
   * three 8-bit deltas. Each delta must land in [-50, -1] LSB to pass.
   * Restores the previous mode afterwards.
   * Returns 0 if all three axes pass, 1 if any axis is out of range,
   * negative on a bus/timeout error. */
  int selfTest(int8_t delta[3], bool axis_pass[3]);

  /* Raw register access, exposed for diagnostics and the `mreg` command. */
  int readReg(uint8_t reg, uint8_t &value);
  int writeReg(uint8_t reg, uint8_t value);

  /* Current configuration, as cached from the last successful write. */
  QMC6309Mode     mode() const { return mode_; }
  QMC6309Osr1     osr1() const { return osr1_; }
  QMC6309Osr2     osr2() const { return osr2_; }
  QMC6309Odr      odr() const { return odr_; }
  QMC6309Range    range() const { return range_; }
  QMC6309SetReset setResetMode() const { return sr_; }
  uint8_t         chipId() const { return chip_id_; }

  /* Sensitivity in LSB per Gauss for the active range (datasheet Table 2). */
  float sensitivityLsbPerGauss() const;
  /* Convenience: LSB per microtesla (1 Gauss = 100 uT). */
  float sensitivityLsbPerMicroTesla() const { return sensitivityLsbPerGauss() / 100.0f; }

  /* Human-readable strings for the cached configuration. */
  const char *modeName() const;
  const char *rangeName() const;
  uint16_t    odrHz() const;
  uint8_t     osr1Ratio() const;
  uint8_t     osr2Ratio() const;
  const char *setResetName() const;

 private:
  TwoWire *wire_;
  uint8_t  addr_;
  uint8_t  chip_id_ = 0;

  /* Cached shadow of the two control registers so read-modify-write does
   * not need a bus read for every field change. */
  uint8_t ctrl1_shadow_ = 0;
  uint8_t ctrl2_shadow_ = 0;

  QMC6309Mode     mode_  = QMC6309_MODE_SUSPEND;
  QMC6309Osr1     osr1_  = QMC6309_OSR1_8;
  QMC6309Osr2     osr2_  = QMC6309_OSR2_8;
  QMC6309Odr      odr_   = QMC6309_ODR_200HZ;
  QMC6309Range    range_ = QMC6309_RANGE_8G;
  QMC6309SetReset sr_    = QMC6309_SR_SET_AND_RESET_ON;

  int writeCtrl1(uint8_t value);
  int writeCtrl2(uint8_t value);
};

#endif  // QMC6309_H
