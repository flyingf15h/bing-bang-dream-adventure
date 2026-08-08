/*
 * protocol.h - serial line protocol shared by the firmware and the
 * Python dashboard (see dashboard/bbda/link.py).
 *
 * Everything is newline-terminated ASCII. The first character of a line
 * identifies the record type, so a parser never has to guess and a human can
 * still read the stream.
 *
 * Two transports carry it, and they carry exactly the same bytes:
 *
 *   USB CDC at 921600 baud.
 *   UDP, port 3333 by default, once the board has WiFi credentials.
 *
 * Over UDP, lines are packed into datagrams of up to 1200 bytes, flushed
 * whenever the buffer fills or the oldest line in it is 10 ms old. A datagram
 * therefore holds whole lines in almost every case, but a reader must still
 * assemble lines across datagram boundaries rather than assuming one datagram
 * is one line. Datagrams may be lost or reordered and nothing retransmits
 * them; every record carries the device timestamp, so loss shows up as a gap
 * rather than as bad data.
 *
 * Commands are accepted from either transport, and a datagram may contain
 * several of them. Unless a destination has been pinned with `udp host`, the
 * board streams to whichever address last sent it a command.
 *
 *   D,<t_us>,ax,ay,az,gx,gy,gz,mx,my,mz,temp_c,mag_fresh
 *       One sample. Accelerometer in g, gyroscope in dps, magnetometer
 *       in microtesla, temperature in degrees C. mag_fresh is 1 when the
 *       magnetometer produced a new sample for this record and 0 when
 *       the previous magnetometer value was repeated.
 *
 *       By default these are RAW readings, so the host can compute a
 *       calibration from them. `csvcal on` makes the firmware apply its
 *       stored calibration to this record instead.
 *
 *   E,<t_us>,<event>,<detail>
 *       An APEX or wake-on-motion event. <event> is one of:
 *       tilt, pedometer, tap, r2w, freefall, lowg, highg, wom.
 *       <detail> is a free-form "key=value key=value" string.
 *
 *   I,<key>,<value>
 *       An informational key/value pair, emitted by `info` and `cal show`
 *       while the output mode is csv.
 *
 *   OK / OK <text>        command succeeded
 *   ERR <text>            command failed
 *
 * Any other line (banners, help text, the pretty-printed sample blocks)
 * is human-facing and should be ignored by a machine parser.
 */

#ifndef BBDA_PROTOCOL_H
#define BBDA_PROTOCOL_H

#define BBDA_PROTOCOL_VERSION 1

#define BBDA_REC_DATA  'D'
#define BBDA_REC_EVENT 'E'
#define BBDA_REC_INFO  'I'

#endif  // BBDA_PROTOCOL_H
