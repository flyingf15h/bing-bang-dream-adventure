/*
 * qmc6309.cpp - QST QMC6309 3-axis magnetometer driver
 * See qmc6309.h for the datasheet reference.
 */

#include "qmc6309.h"

/* Field masks and shifts, from datasheet Tables 15 and 16. */
#define CTRL1_MODE_MASK  0x03
#define CTRL1_MODE_SHIFT 0
#define CTRL1_OSR1_MASK  0x18
#define CTRL1_OSR1_SHIFT 3
#define CTRL1_OSR2_MASK  0xE0
#define CTRL1_OSR2_SHIFT 5

#define CTRL2_SR_MASK     0x03
#define CTRL2_SR_SHIFT    0
#define CTRL2_RNG_MASK    0x0C
#define CTRL2_RNG_SHIFT   2
#define CTRL2_ODR_MASK    0x70
#define CTRL2_ODR_SHIFT   4
#define CTRL2_SOFT_RST    0x80

int QMC6309::readReg(uint8_t reg, uint8_t &value) {
  wire_->beginTransmission(addr_);
  wire_->write(reg);
  if (wire_->endTransmission(false) != 0) return -1;
  if (wire_->requestFrom((int)addr_, 1) != 1) return -2;
  value = wire_->read();
  return 0;
}

int QMC6309::writeReg(uint8_t reg, uint8_t value) {
  wire_->beginTransmission(addr_);
  wire_->write(reg);
  wire_->write(value);
  return wire_->endTransmission() == 0 ? 0 : -1;
}

int QMC6309::writeCtrl1(uint8_t value) {
  int rc = writeReg(QMC6309_REG_CTRL1, value);
  if (rc == 0) ctrl1_shadow_ = value;
  return rc;
}

int QMC6309::writeCtrl2(uint8_t value) {
  int rc = writeReg(QMC6309_REG_CTRL2, value);
  if (rc == 0) ctrl2_shadow_ = value;
  return rc;
}

int QMC6309::begin() {
  /* The chip ID register is the only reliable presence check. */
  if (readReg(QMC6309_REG_CHIP_ID, chip_id_) != 0) return -1;
  if (chip_id_ != QMC6309_CHIP_ID_VALUE) return -2;

  if (softReset() != 0) return -3;

  /* Defaults chosen for a 9-axis fusion feed: fastest ODR the part
   * supports in Normal mode, mid range so a strong hard-iron offset
   * still fits, and the heaviest filtering that keeps 200 Hz. */
  int rc = 0;
  rc |= setSetResetMode(QMC6309_SR_SET_AND_RESET_ON);
  rc |= setRange(QMC6309_RANGE_8G);
  rc |= setOdr(QMC6309_ODR_200HZ);
  rc |= setOsr1(QMC6309_OSR1_8);
  rc |= setOsr2(QMC6309_OSR2_8);
  rc |= setMode(QMC6309_MODE_NORMAL);
  return rc == 0 ? 0 : -4;
}

int QMC6309::softReset() {
  /* SOFT_RST does not auto-clear, so the datasheet's own example writes
   * 0x80 followed by 0x00 (section 7.6). */
  if (writeReg(QMC6309_REG_CTRL2, CTRL2_SOFT_RST) != 0) return -1;
  delay(10);
  if (writeReg(QMC6309_REG_CTRL2, 0x00) != 0) return -1;
  delay(10);

  /* Registers are back at their POR values. */
  ctrl1_shadow_ = 0x00;
  ctrl2_shadow_ = 0x00;
  mode_  = QMC6309_MODE_SUSPEND;
  osr1_  = QMC6309_OSR1_8;
  osr2_  = QMC6309_OSR2_1;
  odr_   = QMC6309_ODR_1HZ;
  range_ = QMC6309_RANGE_32G;
  sr_    = QMC6309_SR_SET_AND_RESET_ON;
  return 0;
}

int QMC6309::setMode(QMC6309Mode mode) {
  /* Datasheet section 6.1: transitions between the three active modes
   * must pass through Suspend. */
  if (mode != QMC6309_MODE_SUSPEND && mode_ != QMC6309_MODE_SUSPEND && mode != mode_) {
    uint8_t suspend = (uint8_t)(ctrl1_shadow_ & ~CTRL1_MODE_MASK);
    if (writeCtrl1(suspend) != 0) return -1;
    delay(2);
  }
  uint8_t v = (uint8_t)((ctrl1_shadow_ & ~CTRL1_MODE_MASK) |
                        ((mode << CTRL1_MODE_SHIFT) & CTRL1_MODE_MASK));
  if (writeCtrl1(v) != 0) return -1;
  mode_ = mode;
  return 0;
}

int QMC6309::setOsr1(QMC6309Osr1 osr) {
  uint8_t v = (uint8_t)((ctrl1_shadow_ & ~CTRL1_OSR1_MASK) |
                        ((osr << CTRL1_OSR1_SHIFT) & CTRL1_OSR1_MASK));
  if (writeCtrl1(v) != 0) return -1;
  osr1_ = osr;
  return 0;
}

int QMC6309::setOsr2(QMC6309Osr2 osr) {
  uint8_t v = (uint8_t)((ctrl1_shadow_ & ~CTRL1_OSR2_MASK) |
                        ((osr << CTRL1_OSR2_SHIFT) & CTRL1_OSR2_MASK));
  if (writeCtrl1(v) != 0) return -1;
  osr2_ = osr;
  return 0;
}

int QMC6309::setOdr(QMC6309Odr odr) {
  uint8_t v = (uint8_t)((ctrl2_shadow_ & ~CTRL2_ODR_MASK) |
                        ((odr << CTRL2_ODR_SHIFT) & CTRL2_ODR_MASK));
  if (writeCtrl2(v) != 0) return -1;
  odr_ = odr;
  return 0;
}

int QMC6309::setRange(QMC6309Range range) {
  uint8_t v = (uint8_t)((ctrl2_shadow_ & ~CTRL2_RNG_MASK) |
                        ((range << CTRL2_RNG_SHIFT) & CTRL2_RNG_MASK));
  if (writeCtrl2(v) != 0) return -1;
  range_ = range;
  return 0;
}

int QMC6309::setSetResetMode(QMC6309SetReset sr) {
  uint8_t v = (uint8_t)((ctrl2_shadow_ & ~CTRL2_SR_MASK) |
                        ((sr << CTRL2_SR_SHIFT) & CTRL2_SR_MASK));
  if (writeCtrl2(v) != 0) return -1;
  sr_ = sr;
  return 0;
}

int QMC6309::readStatus(QMC6309Status &out) {
  uint8_t v;
  if (readReg(QMC6309_REG_STATUS, v) != 0) return -1;
  out.raw            = v;
  out.data_ready     = (v & QMC6309_STATUS_DRDY) != 0;
  out.overflow       = (v & QMC6309_STATUS_OVFL) != 0;
  out.selftest_ready = (v & QMC6309_STATUS_ST_RDY) != 0;
  out.nvm_ready      = (v & QMC6309_STATUS_NVM_RDY) != 0;
  out.nvm_load_done  = (v & QMC6309_STATUS_NVM_LOAD_DONE) != 0;
  return 0;
}

int QMC6309::readRaw(int16_t out[3]) {
  wire_->beginTransmission(addr_);
  wire_->write(QMC6309_REG_XOUT_L);
  if (wire_->endTransmission(false) != 0) return -1;
  if (wire_->requestFrom((int)addr_, 6) != 6) return -2;

  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = wire_->read();

  /* Little endian, 16-bit two's complement (datasheet section 9.2.1). */
  out[0] = (int16_t)((uint16_t)b[1] << 8 | b[0]);
  out[1] = (int16_t)((uint16_t)b[3] << 8 | b[2]);
  out[2] = (int16_t)((uint16_t)b[5] << 8 | b[4]);
  return 0;
}

int QMC6309::readMicroTesla(float out[3]) {
  QMC6309Status st;
  if (readStatus(st) != 0) return -1;
  if (!st.data_ready) return 1;

  int16_t raw[3];
  if (readRaw(raw) != 0) return -1;

  const float lsb_per_ut = sensitivityLsbPerMicroTesla();
  out[0] = raw[0] / lsb_per_ut;
  out[1] = raw[1] / lsb_per_ut;
  out[2] = raw[2] / lsb_per_ut;
  return 0;
}

int QMC6309::selfTest(int8_t delta[3], bool axis_pass[3]) {
  /* Datasheet section 7.3. Self-test is only valid in Continuous mode. */
  const QMC6309Mode previous = mode_;

  if (setMode(QMC6309_MODE_SUSPEND) != 0) return -1;
  delay(5);
  if (setMode(QMC6309_MODE_CONTINUOUS) != 0) return -1;
  delay(20);  /* let one normal measurement complete */

  if (writeReg(QMC6309_REG_CTRL3, 0x80) != 0) return -1;

  /* The datasheet waits a flat 150 ms; poll ST_RDY instead but keep the
   * same worst case as the timeout. */
  QMC6309Status st;
  bool ready = false;
  for (int i = 0; i < 30; i++) {
    delay(5);
    if (readStatus(st) != 0) return -1;
    if (st.selftest_ready) { ready = true; break; }
  }
  if (!ready) {
    setMode(previous);
    return -2;
  }

  uint8_t x, y, z;
  if (readReg(QMC6309_REG_ST_X, x) != 0) return -1;
  if (readReg(QMC6309_REG_ST_Y, y) != 0) return -1;
  if (readReg(QMC6309_REG_ST_Z, z) != 0) return -1;

  delta[0] = (int8_t)x;
  delta[1] = (int8_t)y;
  delta[2] = (int8_t)z;

  /* Pass window is -50..-1 LSB on every axis (datasheet section 7.3). */
  bool all_pass = true;
  for (int i = 0; i < 3; i++) {
    axis_pass[i] = (delta[i] >= -50 && delta[i] <= -1);
    if (!axis_pass[i]) all_pass = false;
  }

  /* Self-test leaves the part in Suspend; put back what the caller had. */
  mode_ = QMC6309_MODE_SUSPEND;
  setMode(previous);
  return all_pass ? 0 : 1;
}

float QMC6309::sensitivityLsbPerGauss() const {
  switch (range_) {
    case QMC6309_RANGE_16G: return 2000.0f;
    case QMC6309_RANGE_8G:  return 4000.0f;
    case QMC6309_RANGE_32G:
    default:                return 1000.0f;
  }
}

const char *QMC6309::modeName() const {
  switch (mode_) {
    case QMC6309_MODE_NORMAL:     return "Normal";
    case QMC6309_MODE_SINGLE:     return "Single";
    case QMC6309_MODE_CONTINUOUS: return "Continuous";
    case QMC6309_MODE_SUSPEND:
    default:                      return "Suspend";
  }
}

const char *QMC6309::rangeName() const {
  switch (range_) {
    case QMC6309_RANGE_16G: return "+/-16 G";
    case QMC6309_RANGE_8G:  return "+/-8 G";
    case QMC6309_RANGE_32G:
    default:                return "+/-32 G";
  }
}

uint16_t QMC6309::odrHz() const {
  switch (odr_) {
    case QMC6309_ODR_1HZ:   return 1;
    case QMC6309_ODR_10HZ:  return 10;
    case QMC6309_ODR_50HZ:  return 50;
    case QMC6309_ODR_100HZ: return 100;
    case QMC6309_ODR_200HZ:
    default:                return 200;
  }
}

uint8_t QMC6309::osr1Ratio() const {
  switch (osr1_) {
    case QMC6309_OSR1_4: return 4;
    case QMC6309_OSR1_2: return 2;
    case QMC6309_OSR1_1: return 1;
    case QMC6309_OSR1_8:
    default:             return 8;
  }
}

uint8_t QMC6309::osr2Ratio() const {
  switch (osr2_) {
    case QMC6309_OSR2_1:  return 1;
    case QMC6309_OSR2_2:  return 2;
    case QMC6309_OSR2_4:  return 4;
    case QMC6309_OSR2_8:  return 8;
    case QMC6309_OSR2_16:
    default:              return 16;
  }
}

const char *QMC6309::setResetName() const {
  switch (sr_) {
    case QMC6309_SR_SET_ONLY_ON:       return "set only on";
    case QMC6309_SR_SET_AND_RESET_OFF: return "set and reset off";
    case QMC6309_SR_SET_AND_RESET_ON:
    default:                           return "set and reset on";
  }
}
