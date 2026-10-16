"""
Read temperatures from, and control setpoint ramps on, a Lake Shore
Model 336 Temperature Controller over its Ethernet (TCP/IP) interface.

The 336 Ethernet interface listens on TCP port 7777 by default and
speaks a simple ASCII command protocol: commands/queries are terminated
with "\\r\\n", and responses are terminated the same way.

Temperature reads use:

    KRDG? <input>

which returns the input's temperature reading in Kelvin, e.g. "+273.15".
<input> is one of: A, B, C, D, D2, D3, D4 (D2-D4 only exist if the
instrument has the optional 3062 scanner module installed).

Setpoint ramping (see Lakeshore336.ramp_to_setpoint()) uses the
control-loop/output commands instead, addressed by output number
(1 or 2 on this instrument) rather than input letter:

    SETP? <output>              query the control setpoint (K)
    SETP <output>,<value>       set the control setpoint (K)
    RAMP? <output>               query ramp on/off and rate (K/min)
    RAMP <output>,<on>,<rate>    set ramp on/off (0/1) and rate (K/min)
    RANGE? <output>              query heater range (0 = heater off)

Reference: Lake Shore Model 336 Temperature Controller manual,
"Input Query Commands" / "KRDG?", and "Output and Loop Control
Commands" / "SETP", "RAMP", "RANGE".
"""

from __future__ import annotations

import socket
import time
import logging

logger = logging.getLogger(__name__)

# Valid input channel strings on a fully populated 336 (with scanner option).
VALID_CHANNELS = {"A", "B", "C", "D", "D2", "D3", "D4"}

# Control-loop/output designators that support heater-range + setpoint-ramp
# control on this Lake Shore 336 (used by ramp_to_setpoint()). Per this
# instrument's wiring, loop N also reads back as input "N" via KRDG?, so
# the same string is used for both.
VALID_RAMP_CHANNELS = {"1", "2"}

# Valid temperature range (K) for both measurements and setpoints.
TEMP_MIN_K = 75.0
TEMP_MAX_K = 310.0

# Maximum allowed |ramp rate| (K/min) for ramp_to_setpoint().
RAMP_MAX_RATE = 2.0


def _transact(sock, command, expect_response=True):
    """Send one command over an already-open socket; optionally read back
    the \\r\\n-terminated response.

    Raises ConnectionError if the instrument closes the connection before
    sending a terminator, or ValueError if it sends an empty response.
    """
    sock.sendall(f"{command}\r\n".encode("ascii"))
    if not expect_response:
        return None
    response = b""
    while not response.endswith(b"\r\n"):
        chunk = sock.recv(256)
        if not chunk:
            raise ConnectionError("Connection closed by instrument before terminator received.")
        response += chunk
    text = response.decode("ascii", errors="replace").strip()
    if not text:
        raise ValueError("Empty response from instrument.")
    return text


class Lakeshore336:
    """A single input channel of a Lake Shore Model 336 Temperature
    Controller, reachable over its Ethernet (TCP/IP) interface.
    host/port/channel (and retry behavior) are fixed at construction.
    read_temperature() opens its own connection, issues the query, and
    closes it -- no persistent link is held between calls.
    """

    def __init__(
        self,
        host: str,
        port: int,
        channel: str,
        retries: int = 3,
        timeout: float = 5.0,
        retry_delay: float = 1.0,
    ):
        """
        Parameters
        ----------
        host : str
            IP address (or hostname) of the Lake Shore 336.
        port : int
            TCP port of the instrument's Ethernet interface (typically 7777).
        channel : str
            Input channel to query. One of "A", "B", "C", "D", "D2", "D3",
            "D4" (case-insensitive).
        retries : int, optional
            Maximum number of attempts to make before giving up. Must be
            >= 1. Default is 3.
        timeout : float, optional
            Socket connect/send/recv timeout, in seconds, per attempt.
            Default is 5.0.
        retry_delay : float, optional
            Seconds to wait between failed attempts. Default is 1.0.

        Raises
        ------
        ValueError
            If `channel` is not a recognized input string or `retries` < 1.
        """
        channel = channel.strip().upper()
        if channel not in VALID_CHANNELS:
            raise ValueError(
                f"Invalid channel '{channel}'. Must be one of {sorted(VALID_CHANNELS)}."
            )
        if retries < 1:
            raise ValueError("retries must be >= 1")

        self.host = host
        self.port = port
        self.channel = channel
        self.retries = retries
        self.timeout = timeout
        self.retry_delay = retry_delay

    def read_temperature(self) -> float:
        """
        Read the temperature (in Kelvin) from this channel, retrying on
        failure.

        Returns
        -------
        float
            The temperature reading in Kelvin.

        Raises
        ------
        ConnectionError
            If all attempts fail to connect, communicate, or parse a valid
            reading. The original exception from the last attempt is
            chained via `raise ... from err`.
        """
        command = f"KRDG? {self.channel}\r\n".encode("ascii")

        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                sock.settimeout(self.timeout)
                sock.sendall(command)

                # Read until we see the "\r\n" terminator.
                response = b""
                while not response.endswith(b"\r\n"):
                    chunk = sock.recv(256)
                    if not chunk:
                        raise ConnectionError(
                            "Connection closed by instrument before terminator received."
                        )
                    response += chunk

                text = response.decode("ascii", errors="replace").strip()
                if not text:
                    raise ValueError("Empty response from instrument.")

                return float(text)

            except (OSError, ValueError) as err:
                last_error = err
                logger.warning(
                    "Lakeshore 336 read attempt %d/%d on channel %s failed: %s",
                    attempt,
                    self.retries,
                    self.channel,
                    err,
                )
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        raise ConnectionError(
            f"Failed to read temperature from Lakeshore 336 at {self.host}:{self.port} "
            f"channel {self.channel} after {self.retries} attempt(s)."
        ) from last_error

    def ramp_to_setpoint(self, channel: str, setpoint_K: float, rate: float = RAMP_MAX_RATE) -> bool:
        """
        Smoothly ramp a Lake Shore 336 control loop to a new setpoint.

        Parks the setpoint at the loop's actual current temperature, then
        enables ramping at `rate` before moving the setpoint to its real
        target -- so the controller ramps smoothly from "now" to
        `setpoint_K` instead of jumping, and so a failure partway through
        this sequence leaves the loop targeting a temperature it's already
        at (never a stale or wrong one).

        Parameters
        ----------
        channel : str
            Control-loop/output designator ("1" or "2" on this instrument).
            The same string is used for KRDG?, SETP, RAMP, and RANGE.
        setpoint_K : float
            New setpoint, in Kelvin. Must be within [TEMP_MIN_K, TEMP_MAX_K].
        rate : float, optional
            Ramp rate in K/min. abs(rate) must be <= RAMP_MAX_RATE; the
            magnitude is what's sent to the instrument regardless of the
            sign passed in (the 336's RAMP rate field is unsigned -- ramp
            direction follows from the setpoint, not the rate's sign).
            Defaults to RAMP_MAX_RATE.

        Returns
        -------
        bool
            True if the ramp was started and verified. False if the loop's
            heater power is off (a warning is printed; no SETP/RAMP commands
            are sent in that case).

        Raises
        ------
        ValueError
            If `channel` is invalid, `setpoint_K` or the current reading is
            out of [TEMP_MIN_K, TEMP_MAX_K], or abs(rate) > RAMP_MAX_RATE.
        ConnectionError
            If communication with the instrument fails.
        """
        # Make sure the channel name is valid
        channel = channel.strip()
        if channel not in VALID_RAMP_CHANNELS:
            raise ValueError(
                f"Invalid channel '{channel}'. Must be one of {sorted(VALID_RAMP_CHANNELS)}."
            )

        # Make sure the setpoint is a valid temperature
        if not (TEMP_MIN_K <= setpoint_K <= TEMP_MAX_K):
            raise ValueError(
                f"Invalid setpoint {setpoint_K} K. Must be between {TEMP_MIN_K} and {TEMP_MAX_K} K."
            )

        # Make sure abs(rate) <= RAMP_MAX_RATE. From here on we only ever
        # send the magnitude to the instrument (see docstring) -- a
        # negative `rate` is a valid *input* but not a valid thing to transmit.
        if abs(rate) > RAMP_MAX_RATE:
            raise ValueError(
                f"Invalid ramp rate {rate} K/min. abs(rate) must be <= {RAMP_MAX_RATE} K/min."
            )
        rate = abs(rate)

        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)

            # Query, print, and check that the current temperature temp0 is valid
            temp0 = float(_transact(sock, f"KRDG? {channel}"))
            print(f"Current temperature on channel {channel}: {temp0:.3f} K")
            if not (TEMP_MIN_K <= temp0 <= TEMP_MAX_K):
                raise ValueError(
                    f"Current temperature {temp0} K on channel {channel} is out of "
                    f"valid range [{TEMP_MIN_K}, {TEMP_MAX_K}] K."
                )

            # Check that the channel's heater power is on; if not, warn and bail
            heater_range = float(_transact(sock, f"RANGE? {channel}"))
            if heater_range == 0:
                print(f"WARNING: heater power is OFF on channel {channel}; not ramping.")
                return False

            # Turn off setpoint ramping for this channel
            _transact(sock, f"RAMP {channel},0,{rate}", expect_response=False)

            # Set the channel setpoint to temp0 -- parks it at the current
            # reading so nothing moves yet
            _transact(sock, f"SETP {channel},{temp0}", expect_response=False)

            # Turn on setpoint ramping, at `rate` -- RAMP sets the on/off flag
            # and rate together, so the rate is applied here, before the
            # setpoint changes below
            _transact(sock, f"RAMP {channel},1,{rate}", expect_response=False)

            # Change the setpoint to setpoint_K -- ramping is already on and
            # at the right rate, so the loop now ramps smoothly toward it
            _transact(sock, f"SETP {channel},{setpoint_K}", expect_response=False)

            # Read back and print what the instrument actually committed to,
            # rather than assuming the writes above took effect
            applied_setpoint = float(_transact(sock, f"SETP? {channel}"))
            ramp_on, ramp_rate = _transact(sock, f"RAMP? {channel}").split(",")
            print(
                f"Channel {channel} ramping {'ON' if int(float(ramp_on)) else 'OFF'} "
                f"at {float(ramp_rate):.3f} K/min toward setpoint {applied_setpoint:.3f} K"
            )

        except OSError as err:
            raise ConnectionError(
                f"Failed to ramp Lakeshore 336 channel {channel} to {setpoint_K} K: {err}"
            ) from err
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        return True


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)

    lks = Lakeshore336(
        host="192.168.1.100",
        port=7777,
        channel="A",
        retries=5,
        timeout=3.0,
        retry_delay=0.5,
    )
    temp_k = lks.read_temperature()
    print(f"Temperature: {temp_k:.3f} K")
