#!/cygdrive/c/Users/hiro/anaconda3/envs/py38/python
"""
keysight_lan.py - Unified Keysight power supply control module

Usage:
    python keysight_lan.py HOST[:PORT] read [channel list]    e.g. 1,2,3  or  1
    python keysight_lan.py HOST[:PORT] on [channel]
    python keysight_lan.py HOST[:PORT] off [channel]
    python keysight_lan.py HOST[:PORT] v <voltage> [channel]

    For a single channel only (default is ch 1 LED):
    python keysight_lan.py HOST[:PORT] flash <delay_on> <delay_off> <voltage> [channel]

HOST is required. PORT defaults to 5025 (the standard SCPI/VXI-11 port) if
omitted, e.g.:
    python keysight_lan.py 192.168.1.5 read
    python keysight_lan.py 192.168.1.5:5025 read

The leading '@' in channel specs is optional — '1,2,3' and '@1,2,3' both work.
"""

import sys
import pyvisa
from datetime import datetime
#import time


# DEFAULT_PORT is the standard SCPI/VXI-11 port for this instrument family,
# used as a fallback whenever a port isn't given explicitly (e.g. a bare
# HOST with no ':PORT' on the CLI below).
DEFAULT_PORT = 5025

# Alternative: raw SCPI socket on port 5025 instead of VXI-11. Only use this
# if VXI-11 isn't available; it requires setting read/write terminators
# manually in Keysight._connect() below (ps.read_termination = ps.write_termination = '\n'),
# and building the resource string as f'TCPIP0::{host}::{port}::SOCKET'.

# Default LED channel
LED_CHANNEL = 1

def timestamp(suffix=''):
    """Return a formatted timestamp string."""
    now = datetime.now()
    fmt = f"%H:%M:%S {suffix}" if suffix else "%H:%M:%S "
    return now.strftime(fmt)


def normalize_channel(channel):
    """Ensure channel spec has a leading '@', e.g. '1,2,3' -> '@1,2,3'."""
    channel = str(channel).strip()
    return channel if channel.startswith('@') else f'@{channel}'


class Keysight:
    """A single channel of a Keysight power supply, reachable over LAN via
    VISA/VXI-11. host/port/channel are fixed at construction. Each method
    opens its own connection, issues its command(s), and closes it -- no
    persistent link is held between calls.
    """

    def __init__(self, host, port=DEFAULT_PORT, channel=LED_CHANNEL):
        self.host = host
        self.port = port
        self.channel = channel

    def _connect(self):
        """Open and return the instrument resource over LAN.

        Raises ConnectionError on failure (instead of killing the process),
        since this may be called repeatedly from a long-running session.
        """
        # '@py' = pyvisa's pure-Python backend.
        # It talks TCPIP/VXI-11 directly over the network, so no NI-VISA /
        # Keysight IO Libraries / IVI binary needs to be installed on this machine
        resource = f'TCPIP0::{self.host}::{self.port}::INSTR'
        rm = pyvisa.ResourceManager('@py')
        try:
            ps = rm.open_resource(resource)
            # Only needed if using a ...::SOCKET resource instead of VXI-11:
            # ps.read_termination = '\n'
            # ps.write_termination = '\n'
            return ps
        except pyvisa.errors.VisaIOError as e:
            raise ConnectionError(f"Keysight IO error connecting to {resource}: {e}") from e

    def read(self, channel=None):
        """Read and print voltage, current, and output state for this channel."""
        ch = channel if channel else self.channel
        ps = self._connect()
        try:
            ps.write(f'INST:NSEL {ch}')
            state = 'ON' if ps.query('OUTP?').strip() == '1' else 'OFF'
            set_v = float(ps.query('VOLT?').strip())
            v = float(ps.query('MEAS:VOLT?').strip())
            c = float(ps.query('MEAS:CURR?').strip()) * 1000.0  # mA

            print(f"CH{ch}> {timestamp()} set {set_v:6.3f} V  {state:3s}  "
                  f"meas {v:6.3f} V {c:6.3f} mA")
        except pyvisa.errors.VisaIOError as e:
            raise ConnectionError(f"Keysight read error: {e}") from e
        finally:
            ps.close()

              # Von (bool), Vset, Vmeas
        return state=='ON', set_v, v

    def output_on(self):
        """Turn output ON for this channel."""
        ps = self._connect()
        try:
            chan = normalize_channel(self.channel)
            ps.write(f'OUTP ON,({chan})')
            print(timestamp(f'OUTON ({chan})'))
        finally:
            ps.close()

    def output_off(self):
        """Turn output OFF for this channel."""
        ps = self._connect()
        try:
            chan = normalize_channel(self.channel)
            ps.write(f'OUTP OFF,({chan})')
            print(timestamp(f'OUTOFF ({chan})'))
        finally:
            ps.close()

    def set_voltage(self, voltage):
        """Set output voltage on this channel."""
        ps = self._connect()
        try:
            ps.write(f'INST:NSEL {self.channel}')
            ps.write(f'VOLT {voltage}')
            print(timestamp(f'setV {voltage} V (ch{self.channel})'))
        finally:
            ps.close()

    def flash_LED(self, delay_on, delay_off, volt=None):
        """Turn LED ON after delay_on (s) then OFF after delay_off (s).
        Default voltage is the currently stored Keysight setting.
        After flashing, the setting is reset to 0V."""

        V_OFF = 0.
        I_OFF = 0.001  #  1  mA
        I_MAX = 0.1    #100 mA

        ps = self._connect()
        try:
            ps.write(f'INST:NSEL {self.channel}')  #  select channel for subsequent commands

            if volt is None:
                volt = float(ps.query('VOLT?'))  # Save settings
                curr = float(ps.query('CURR?'))
            else:
                curr = I_MAX

            print(timestamp(f"Flashing LED: After {delay_on}s, do {volt}V for "
                             f"{delay_off}s on ch{self.channel}"))

            #time.sleep(1)
            ps.write('OUTP OFF')  # start in fully off state

            command_list = [
                'VOLT:MODE LIST',
                'CURR:MODE LIST',
                f'LIST:CURR {I_OFF}, {I_MAX}, {I_OFF}',       # off, max, off
                f'LIST:VOLT {V_OFF}, {volt}, {V_OFF}',        # 0V off, V on, 0V off
                f'LIST:DWEL {delay_on}, {delay_off}, 0.01',   # off, on, off

                'LIST:COUNT 1',         # Run list 1 time
                'LIST:TERM:LAST ON',    # ON = stay in final list state; OFF = return to previous state
                'LIST:STEP AUTO',
                'TRIG:SOUR IMM',
                'OUTP ON',              # Must be on for list to control output; starts at 0V
                'INIT',                 # Start the sequence
            ]

            for cmd in command_list:
                # print(cmd)
                ps.write(cmd)

            # Finish by resetting mode #### OK?
            # ps.write('VOLT:MODE FIX')
            # ps.write('CURR:MODE FIX')
        finally:
            ps.close()


def _parse_host_port(spec):
    """Split a 'HOST' or 'HOST:PORT' CLI token into (host, port).
    Defaults to DEFAULT_PORT (5025) if no ':PORT' is given.
    """
    host, _, port = spec.partition(':')
    return host, (int(port) if port else DEFAULT_PORT)


def main():
    '''Access basic functions from the command line'''
    
    args = sys.argv[1:]

    if len(args) < 2:
        print(__doc__)
        sys.exit(0)

    host, port = _parse_host_port(args[0])
    command = args[1].lower()
    cmd_args = args[2:]

    try:
        if command == 'read':
            channels = cmd_args[0].split(',') if cmd_args else [LED_CHANNEL]
            for ch in channels:
                Keysight(host, port, ch).read()

        elif command == 'on':
            channel = cmd_args[0] if cmd_args else LED_CHANNEL
            Keysight(host, port, channel).output_on()

        elif command == 'off':
            channel = cmd_args[0] if cmd_args else LED_CHANNEL
            Keysight(host, port, channel).output_off()

        elif command == 'v':
            if not cmd_args:
                print("Usage: keysight_lan.py HOST[:PORT] v <voltage> [channel_num]")
                sys.exit(1)
            voltage = cmd_args[0]
            channel = cmd_args[1] if len(cmd_args) > 1 else LED_CHANNEL
            Keysight(host, port, channel).set_voltage(voltage)

        elif command == 'flash':
            if len(cmd_args) < 3:
                print("Usage: keysight_lan.py HOST[:PORT] flash <delay_on> <delay_off> <voltage> [channel]")
                sys.exit(1)
            delay_on, delay_off, volt = cmd_args[0], cmd_args[1], cmd_args[2]
            channel = cmd_args[3] if len(cmd_args) > 3 else LED_CHANNEL
            Keysight(host, port, channel).flash_LED(delay_on, delay_off, volt)

        else:
            print(f"Unknown command: '{command}'")
            print(__doc__)
            sys.exit(1)

    except ConnectionError as e:
        print(f"Keysight connection error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
