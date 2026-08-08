"""Orientation estimation.

Madgwick's gradient-descent AHRS filter, in both its 9-axis (accelerometer +
gyroscope + magnetometer) and 6-axis forms. The 6-axis form is used
automatically whenever the magnetometer sample is unusable, which keeps roll
and pitch alive even with the magnetometer disconnected or saturated.

Frame convention
----------------
The quaternion carried by the filter is the orientation of the *sensor*
frame relative to the *earth* frame: ``quat_to_matrix(q)`` maps a vector
expressed in board axes into world axes, which is what a renderer and a
human both want. That is the convention the integration step
``qdot = 0.5 * q * (0, omega_body)`` and the two objective functions in this
module are all written in, so no conjugation happens anywhere.

The world frame is x = magnetic north, y = west, z = up. A board lying flat
with its z axis up and its x axis pointing at magnetic north has the identity
orientation: roll 0, pitch 0, heading 0.
"""

from __future__ import annotations

import math

import numpy as np


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """Rotation matrix for the quaternion ``q = [w, x, y, z]``."""
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


class MadgwickAHRS:
    """Madgwick's filter with the optional gyroscope-drift term.

    Parameters
    ----------
    beta:
        Filter gain. Trades convergence speed against how much accelerometer
        and magnetometer noise leaks into the estimate. 0.03-0.1 suits a
        static or slow-moving board; raise it when the board is being handled.
    zeta:
        Gain of the gyroscope bias tracker. Zero disables bias tracking, which
        is the right choice once the gyroscope has been calibrated properly.
    """

    def __init__(self, beta: float = 0.05, zeta: float = 0.0) -> None:
        self.beta = beta
        self.zeta = zeta
        self.q = np.array([1.0, 0.0, 0.0, 0.0])
        self.gyro_bias = np.zeros(3)   # rad/s, tracked when zeta > 0

    def reset(self) -> None:
        self.q = np.array([1.0, 0.0, 0.0, 0.0])
        self.gyro_bias = np.zeros(3)

    # ------------------------------------------------------------------
    def update(
        self,
        gyro_dps: np.ndarray,
        accel_g: np.ndarray,
        mag_ut: np.ndarray | None,
        dt: float,
    ) -> np.ndarray:
        """Advance the filter one step and return the current quaternion.

        ``mag_ut`` may be None or a zero vector, in which case the 6-axis
        update runs instead.
        """
        if dt <= 0 or dt > 1.0:
            # A gap this large means dropped samples; integrating across it
            # would fling the estimate, so skip the step instead.
            return self.q

        gyro = np.radians(np.asarray(gyro_dps, dtype=float))
        accel = np.asarray(accel_g, dtype=float)

        a_norm = np.linalg.norm(accel)
        if a_norm < 1e-6:
            # No usable gravity reference: integrate the gyroscope alone.
            self._integrate(gyro, dt)
            return self.q
        accel = accel / a_norm

        use_mag = mag_ut is not None
        if use_mag:
            mag = np.asarray(mag_ut, dtype=float)
            m_norm = np.linalg.norm(mag)
            use_mag = m_norm > 1e-6
            if use_mag:
                mag = mag / m_norm

        if use_mag:
            grad = self._gradient_9axis(accel, mag)
        else:
            grad = self._gradient_6axis(accel)

        grad_norm = np.linalg.norm(grad)
        if grad_norm > 1e-12:
            grad = grad / grad_norm

            if self.zeta > 0:
                # The gradient expressed as an angular rate is the residual
                # the gyroscope failed to explain, i.e. its bias.
                q = self.q
                w_err = 2.0 * np.array(
                    [
                        q[0] * grad[1] - q[1] * grad[0] - q[2] * grad[3] + q[3] * grad[2],
                        q[0] * grad[2] + q[1] * grad[3] - q[2] * grad[0] - q[3] * grad[1],
                        q[0] * grad[3] - q[1] * grad[2] + q[2] * grad[1] - q[3] * grad[0],
                    ]
                )
                self.gyro_bias += w_err * dt * self.zeta
                gyro = gyro - self.gyro_bias

        q_dot = 0.5 * self._quat_mult(self.q, np.array([0.0, *gyro]))
        if grad_norm > 1e-12:
            q_dot = q_dot - self.beta * grad

        self.q = self.q + q_dot * dt
        self.q /= np.linalg.norm(self.q)
        return self.q

    def _integrate(self, gyro: np.ndarray, dt: float) -> None:
        q_dot = 0.5 * self._quat_mult(self.q, np.array([0.0, *gyro]))
        self.q = self.q + q_dot * dt
        self.q /= np.linalg.norm(self.q)

    @staticmethod
    def _quat_mult(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        w1, x1, y1, z1 = a
        w2, x2, y2, z2 = b
        return np.array(
            [
                w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            ]
        )

    def _gradient_6axis(self, a: np.ndarray) -> np.ndarray:
        """Gradient of the gravity-only objective function."""
        q0, q1, q2, q3 = self.q
        ax, ay, az = a

        f = np.array(
            [
                2 * (q1 * q3 - q0 * q2) - ax,
                2 * (q0 * q1 + q2 * q3) - ay,
                2 * (0.5 - q1 * q1 - q2 * q2) - az,
            ]
        )
        j = np.array(
            [
                [-2 * q2, 2 * q3, -2 * q0, 2 * q1],
                [2 * q1, 2 * q0, 2 * q3, 2 * q2],
                [0.0, -4 * q1, -4 * q2, 0.0],
            ]
        )
        return j.T @ f

    def _gradient_9axis(self, a: np.ndarray, m: np.ndarray) -> np.ndarray:
        """Gradient of the combined gravity + magnetic-field objective.

        The earth-frame magnetic reference is recomputed every step by
        rotating the measurement into the earth frame and flattening it into
        the x-z plane, which is what makes the filter immune to declination.
        """
        q0, q1, q2, q3 = self.q
        ax, ay, az = a
        mx, my, mz = m

        # Rotate the measured field into the earth frame: h = q (x) m (x) q*
        h = self._quat_mult(
            self._quat_mult(self.q, np.array([0.0, mx, my, mz])), quat_conjugate(self.q)
        )
        bx = math.hypot(h[1], h[2])
        bz = h[3]

        f = np.array(
            [
                2 * (q1 * q3 - q0 * q2) - ax,
                2 * (q0 * q1 + q2 * q3) - ay,
                2 * (0.5 - q1 * q1 - q2 * q2) - az,
                2 * bx * (0.5 - q2 * q2 - q3 * q3) + 2 * bz * (q1 * q3 - q0 * q2) - mx,
                2 * bx * (q1 * q2 - q0 * q3) + 2 * bz * (q0 * q1 + q2 * q3) - my,
                2 * bx * (q0 * q2 + q1 * q3) + 2 * bz * (0.5 - q1 * q1 - q2 * q2) - mz,
            ]
        )
        j = np.array(
            [
                [-2 * q2, 2 * q3, -2 * q0, 2 * q1],
                [2 * q1, 2 * q0, 2 * q3, 2 * q2],
                [0.0, -4 * q1, -4 * q2, 0.0],
                [-2 * bz * q2, 2 * bz * q3, -4 * bx * q2 - 2 * bz * q0,
                 -4 * bx * q3 + 2 * bz * q1],
                [-2 * bx * q3 + 2 * bz * q1, 2 * bx * q2 + 2 * bz * q0,
                 2 * bx * q1 + 2 * bz * q3, -2 * bx * q0 + 2 * bz * q2],
                [2 * bx * q2, 2 * bx * q3 - 4 * bz * q1, 2 * bx * q0 - 4 * bz * q2,
                 2 * bx * q1],
            ]
        )
        return j.T @ f

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    def orientation_quaternion(self) -> np.ndarray:
        """Board orientation in the world frame, as ``[w, x, y, z]``."""
        return self.q.copy()

    def rotation_matrix(self) -> np.ndarray:
        """Matrix that maps a vector in board axes into world axes."""
        return quat_to_matrix(self.q)

    def euler_degrees(self) -> tuple[float, float, float]:
        """Roll, pitch and yaw of the board, in degrees.

        Yaw is measured counter-clockwise from magnetic north looking down
        the world z axis; see :meth:`heading_degrees` for the compass form.
        """
        r = self.rotation_matrix()
        roll = math.atan2(r[2, 1], r[2, 2])
        pitch = -math.asin(max(-1.0, min(1.0, r[2, 0])))
        yaw = math.atan2(r[1, 0], r[0, 0])
        return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)

    def heading_degrees(self) -> float:
        """Compass heading of the board's x axis: 0 = north, 90 = east."""
        _, _, yaw = self.euler_degrees()
        return (-yaw) % 360.0


def tilt_from_accel(accel_g: np.ndarray) -> tuple[float, float]:
    """Roll and pitch straight from gravity, in degrees.

    Independent of the filter, so it is a useful sanity check while tuning:
    with the board held still the filter output should agree with this.
    """
    ax, ay, az = accel_g
    roll = math.atan2(ay, az)
    pitch = math.atan2(-ax, math.hypot(ay, az))
    return math.degrees(roll), math.degrees(pitch)
