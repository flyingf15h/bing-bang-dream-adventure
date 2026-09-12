class_name Wire
extends RefCounted
## The bridge <-> game wire format, v2. Mirrors bridge/bbda/protocol.py
## field-for-field -- if one side changes a name here, the other has to as
## well, or they drift apart silently. See that file's docstring for the full
## record shapes and the reasoning behind each one.
##
## Not an autoload: this holds no state, only names, so it is referenced as
## ``Wire.TYPE_FLICK`` etc. wherever a record or command is read or built,
## instead of a bare string literal that a typo could slip past unnoticed.

const WIRE_VERSION := 2

## --- record types: bridge -> game ------------------------------------------
const TYPE_HELLO := "hello"
const TYPE_STATUS := "status"
const TYPE_BYE := "bye"
const TYPE_FLICK := "flick"
const TYPE_MOTION := "motion"
const TYPE_REFUSED := "refused"
const TYPE_CONFIG := "config"
const TYPE_FRONT_SUGGESTION := "front_suggestion"
const TYPE_LEARNING := "learning"
const TYPE_REST := "rest"
const TYPE_MEASURING := "measuring"
const TYPE_BIAS_WRITTEN := "bias_written"
const TYPE_BOARD_CAL := "board_cal"
## New in v2.
const TYPE_SCAN := "scan"
const TYPE_CAL_STATE := "cal_state"
const TYPE_CAL_DONE := "cal_done"
const TYPE_TRANSPORT := "transport"

## --- commands: game -> bridge control port ----------------------------------
const CMD_GET := "get"
const CMD_SET := "set"
const CMD_LEARN_FRONT := "learn_front"
const CMD_MEASURE_REST := "measure_rest"
const CMD_WRITE_BIAS := "write_bias"
const CMD_RESET := "reset"
## New in v2.
const CMD_SCAN := "scan"
const CMD_TRANSPORT := "transport"
const CMD_CAL_START := "cal_start"
const CMD_CAL_ADVANCE := "cal_advance"
const CMD_CAL_CANCEL := "cal_cancel"
const CMD_CAL_SAVE := "cal_save"
