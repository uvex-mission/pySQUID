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
    RANGE? <output>              query heater range (0=off, 1-3=LOW/MED/HIGH)
    HTR? <output>                query heater output, % of selected range's max

Loop/input introspection (see Lakeshore336.loop_info()) additionally uses:

    OUTMODE? <output>            query <mode>,<input code>,<powerup enable>
                                  -- <input code> is numeric (see
                                  OUTMODE_INPUT_CODES), not a letter
    INNAME? <input>              query the saved hardware name for an input

Reference: Lake Shore Model 336 Temperature Controller manual,
"Input Query Commands" / "KRDG?", "INNAME?", and "Output and Loop
Control Commands" / "SETP", "RAMP", "RANGE", "HTR?", "OUTMODE".
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

# Numeric <input> codes used by the OUTMODE command's control-input field
# (Lake Shore 336 manual, "OUTMODE" command) -- a different encoding than
# the input letters used everywhere else (KRDG?, INNAME?, ...), because
# OUTMODE needs to represent "no input assigned" (code 0).
OUTMODE_INPUT_CODES = {0: None, 1: "A", 2: "B", 3: "C", 4: "D", 5: "D2", 6: "D3", 7: "D4"}

# Heater range setting labels (RANGE command) for this instrument's
# heater outputs -- a discrete OFF/LOW/MED/HIGH selector (0-3), not a
# linear percentage of max output.
RANGE_LABELS = {0: "OFF", 1: "LOW", 2: "MED", 3: "HIGH"}


def _transact(sock, command, expect_response=True, allow_empty=False):
    """Send one command over an already-open socket; optionally read back
    the \\r\\n-terminated response.

    Raises ConnectionError if the instrument closes the connection before
    sending a terminator. Raises ValueError if it sends an empty response,
    unless allow_empty=True (needed for queries like INNAME? that can
    legitimately return an empty string).
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
    if not text and not allow_empty:
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

    def loop_info(self, channel: str | int) -> None:
        """
        Print information about a Lake Shore 336 control loop or input
        channel, depending on which kind of channel string is given, as
        one standardized line per input/loop pairing:

            INPUT: <letter or NONE>   NAME: <saved name or NONE>   TEMP: <reading or NONE>   LOOP: <designator or NONE>   SET: <setpoint or NONE>   HEAT: <ON/OFF or NONE>   PWR: <percent of range label, or NONE>

        If `channel` is a valid control-loop designator (1 or 2, as a str
        or int), prints a single row: the input that loop reads from
        (INPUT: NONE if none assigned) with its saved hardware name and
        current temperature (KRDG?), and that loop's setpoint (SETP?),
        heater ON/OFF state (RANGE?), and output power as a percentage of
        the currently selected range's max, labeled OFF/LOW/MED/HIGH (HTR?).

        If `channel` is a valid input channel letter (A, B, C, D, D2, D3,
        D4), prints that input's saved hardware name and current
        temperature, then one row per control loop currently reading from
        it (each with its own setpoint/heater/power) -- or a single row
        with LOOP/SET/HEAT/PWR all NONE if no loop uses it.

        Parameters
        ----------
        channel : str or int
            Either a control-loop designator (1, 2, "1", or "2") or an
            input channel letter (A, B, C, D, D2, D3, D4).

        Raises
        ------
        ValueError
            If `channel` is neither a valid loop designator nor a valid
            input channel letter.
        ConnectionError
            If communication with the instrument fails.
        """
        channel = str(channel).strip()
        channel_upper = channel.upper()

        is_loop = channel in VALID_RAMP_CHANNELS
        is_input = channel_upper in VALID_CHANNELS

        if not is_loop and not is_input:
            raise ValueError(
                f"Invalid channel '{channel}'. Must be a loop designator "
                f"{sorted(VALID_RAMP_CHANNELS)} or an input channel {sorted(VALID_CHANNELS)}."
            )

        def _row(input_val, name_val, temp_str, loop_val, set_str, heat_val, pwr_str):
            print(
                f"INPUT: {input_val or 'NONE'}   "
                f"NAME: {name_val or 'NONE'}   "
                f"TEMP: {temp_str or 'NONE'}   "
                f"LOOP: {loop_val or 'NONE'}   "
                f"SET: {set_str or 'NONE'}   "
                f"HEAT: {heat_val or 'NONE'}   "
                f"PWR: {pwr_str or 'NONE'}"
            )

        def _heater_status(sock, loop):
            """Query heater ON/OFF and range label (RANGE?), and output
            power as a % of that range's max (HTR?), for a loop."""
            range_code = int(float(_transact(sock, f"RANGE? {loop}")))
            status = "OFF" if range_code == 0 else "ON"
            range_label = RANGE_LABELS.get(range_code, str(range_code))
            power = float(_transact(sock, f"HTR? {loop}"))
            return status, range_label, power

        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)

            if is_loop:
                # Valid control loop channel: one row for the input it uses
                _, input_code, _ = _transact(sock, f"OUTMODE? {channel}").split(",")
                input_letter = OUTMODE_INPUT_CODES.get(int(input_code))
                if input_letter:
                    name = _transact(sock, f"INNAME? {input_letter}", allow_empty=True)
                    temp = float(_transact(sock, f"KRDG? {input_letter}"))
                    temp_str = f"{temp:.3f} K"
                else:
                    name = None
                    temp_str = None

                setpoint = float(_transact(sock, f"SETP? {channel}"))
                status, range_label, power = _heater_status(sock, channel)
                _row(
                    input_letter, name, temp_str, channel,
                    f"{setpoint:.3f} K", status, f"{power:.2f}% of {range_label}"
                )

            else:
                # Valid channel name: its saved name and current temperature,
                # plus a row per loop using it
                name = _transact(sock, f"INNAME? {channel_upper}", allow_empty=True)
                temp = float(_transact(sock, f"KRDG? {channel_upper}"))
                temp_str = f"{temp:.3f} K"

                used_by = []
                for loop in sorted(VALID_RAMP_CHANNELS):
                    _, input_code, _ = _transact(sock, f"OUTMODE? {loop}").split(",")
                    if OUTMODE_INPUT_CODES.get(int(input_code)) == channel_upper:
                        used_by.append(loop)

                if used_by:
                    for loop in used_by:
                        setpoint = float(_transact(sock, f"SETP? {loop}"))
                        status, range_label, power = _heater_status(sock, loop)
                        _row(
                            channel_upper, name, temp_str, loop,
                            f"{setpoint:.3f} K", status, f"{power:.2f}% of {range_label}"
                        )
                else:
                    _row(channel_upper, name, temp_str, None, None, None, None)

        except OSError as err:
            raise ConnectionError(
                f"Failed to query Lakeshore 336 loop/channel info for '{channel}': {err}"
            ) from err
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


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
