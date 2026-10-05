"""
Read a temperature from a Lake Shore Model 336 Temperature Controller
over its Ethernet (TCP/IP) interface.

The 336 Ethernet interface listens on TCP port 7777 by default and
speaks a simple ASCII command protocol: commands/queries are terminated
with "\\r\\n", and responses are terminated the same way.

The relevant query is:

    KRDG? <input>

which returns the input's temperature reading in Kelvin, e.g. "+273.15".
<input> is one of: A, B, C, D, D2, D3, D4 (D2-D4 only exist if the
instrument has the optional 3062 scanner module installed).

Reference: Lake Shore Model 336 Temperature Controller manual,
"Input Query Commands" / "KRDG?".
"""

from __future__ import annotations

import socket
import time
import logging

logger = logging.getLogger(__name__)

# Valid input channel strings on a fully populated 336 (with scanner option).
VALID_CHANNELS = {"A", "B", "C", "D", "D2", "D3", "D4"}


def read_lakeshore336_temperature(
    ip_address: str,
    port: int,
    channel: str,
    retries: int = 3,
    timeout: float = 5.0,
    retry_delay: float = 1.0,
) -> float:
    """
    Read the temperature (in Kelvin) from a single input channel of a
    Lake Shore 336 temperature controller over Ethernet, retrying on
    failure.

    Parameters
    ----------
    ip_address : str
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

    Returns
    -------
    float
        The temperature reading in Kelvin.

    Raises
    ------
    ValueError
        If `channel` is not a recognized input string or `retries` < 1.
    ConnectionError
        If all attempts fail to connect, communicate, or parse a valid
        reading. The original exception from the last attempt is
        chained via `raise ... from err`.
    """
    channel = channel.strip().upper()
    if channel not in VALID_CHANNELS:
        raise ValueError(
            f"Invalid channel '{channel}'. Must be one of {sorted(VALID_CHANNELS)}."
        )
    if retries < 1:
        raise ValueError("retries must be >= 1")

    command = f"KRDG? {channel}\r\n".encode("ascii")

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((ip_address, port), timeout=timeout)
            sock.settimeout(timeout)
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
                retries,
                channel,
                err,
            )
            if attempt < retries:
                time.sleep(retry_delay)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    raise ConnectionError(
        f"Failed to read temperature from Lakeshore 336 at {ip_address}:{port} "
        f"channel {channel} after {retries} attempt(s)."
    ) from last_error


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)

    temp_k = read_lakeshore336_temperature(
        ip_address="192.168.1.100",
        port=7777,
        channel="A",
        retries=5,
        timeout=3.0,
        retry_delay=0.5,
    )
    print(f"Temperature: {temp_k:.3f} K")
