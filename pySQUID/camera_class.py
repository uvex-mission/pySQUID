#!/disk/bifrost/uvexdet/miniconda3/bin/python
'''
Class for sending commands to the camera server
'''

# TODO:
# Update hardcoded overhead timing estimate
# Get safe bias ranges
# Fix timeouts -- have some short default for all commands except exposures
# UNTESTED: set_hardware_window <start row> <end row>

from datetime import datetime
import os
import socket
import sys
import threading
import time
import yaml

from .lakeshore336 import read_lakeshore336_temperature
from . import keysight_lan

TO_DEFAULT = 3 # Default timeout (s) for server connections and commands
LKS_RETRIES = 3 # Default retry count for Lakeshore 336 temperature reads

# Safety limits; ### TBC
VMIN, VMAX = (0,3.3)

NICARD_DELAY = 2.   # Delay between sending "expose" and start of 1st frame scan
WRITE_DELAY_S = 11. # Overhead to write data file
SCANTIME_S   = 8.5  # Aproximate FULL-FRAME scan time ### Different for each mode
MARGIN_S     = 2

TT_RESTART_S = 34  # Approx time to restart BBX (load + init)
TT_LEDSTATE_S = 1  # Approx time to get LED state

# Deferred import: 
# camera_deprecated.py imports constants from this module, so this must come after
# those constants are defined above, not at the very top of the file with
# the other imports -- this is what breaks the otherwise-circular import.
from .camera_deprecated import _DeprecatedSquidLED

### This needs to come from a table of known modes and properties
def overhead(nexp, mode=None):
    ''' Estimate overhead (s) for an exposure series'''
    t = 12.5*(nexp+1) + 13
    return t

# Can't proceed unless these exist in user's config file
YAML_REQUIRED_KEYS = ['OPERATOR', 'TESTBED', 'DETID', 'DETTYPE', 'DETCTRL', 'LEDWAVE']

# Per-testbed settings (HOST, PORT, ...) that must be known either from
# testbeds.yaml or from the user's own config file (which takes priority)
TESTBED_REQUIRED_KEYS = ['HOST', 'PORT']

# Optional per-testbed Lakeshore 336 settings 
# If LKS_HOST IS given, LKS_PORT and LKS_CHAN must be given too
LKS_OPTIONAL_KEYS = ['LKS_HOST', 'LKS_PORT', 'LKS_CHAN']

# Optional per-testbed Keysight power supply settings 
# If KEYSIGHT_HOST IS given, KEYSIGHT_PORT and KEYSIGHT_CHAN must be given too
KEYSIGHT_OPTIONAL_KEYS = ['KEYSIGHT_HOST', 'KEYSIGHT_PORT', 'KEYSIGHT_CHAN']

# Per-device settings (VEXTRAHI, VEXTRALO, ...) looked up from
# devices.yaml by DETID. Unlike TESTBED_REQUIRED_KEYS, these always end up
# with a value -- if devices.yaml is missing an entry for a DETID, its
# DEFAULT entry is used instead (with a warning).
DEVICE_KEYS = ['VEXTRAHI', 'VEXTRALO']

# Reserved key in devices.yaml holding fallback values for any DETID not
# otherwise listed there
DEVICES_DEFAULT_KEY = 'DEFAULT'

PROTECTED_KEYS = ['USER', 'SUBDIR']
PROTECTED_KEYS += YAML_REQUIRED_KEYS
PROTECTED_KEYS += TESTBED_REQUIRED_KEYS
PROTECTED_KEYS += LKS_OPTIONAL_KEYS
PROTECTED_KEYS += KEYSIGHT_OPTIONAL_KEYS
PROTECTED_KEYS += DEVICE_KEYS

# Sidecar YAML files supplying default settings for known testbeds/devices
_TESTBEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "testbeds.yaml")
_DEVICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "devices.yaml")


def get_testbed_defaults(testbed, testbedsFile=_TESTBEDS_PATH):
    '''Look up the default settings (HOST, PORT, ...) for a named testbed.

    testbed:     TESTBED name, as it would appear in a user's config file
    testbedsFile: path to the YAML file mapping testbed names to their
                  settings (defaults to testbeds.yaml shipped alongside
                  this module)

    Returns a dict of settings for that testbed. Unlike get_device_defaults,
    there's no generic fallback for an unrecognized TESTBED: a warning is
    printed and an exception is raised immediately.
    '''
    with open(testbedsFile, 'r') as file:
        testbeds = yaml.safe_load(file) or {}

    if testbed not in testbeds:
        print(f"\n\nWARNING: TESTBED '{testbed}' is not a known testbed name.")
        raise Exception(
            f"Please add TESTBED '{testbed}' to {testbedsFile}, or use a "
            f"known testbed name in your config file."
        )

    return testbeds[testbed]


def get_device_defaults(detid, devicesFile=_DEVICES_PATH):
    '''Look up the default per-device settings (VEXTRAHI, VEXTRALO, ...)
    for a DETID.

    detid:       DETID value, as it would appear in a user's config file
    devicesFile: path to the YAML file mapping DETIDs to their settings
                 (defaults to devices.yaml shipped alongside this module)

    Returns a dict of settings for that device. If devicesFile has no
    entry for the given DETID, a warning is printed, the file's
    DEVICES_DEFAULT_KEY ('DEFAULT') entry is offered as a fallback, and
    the user is prompted to confirm before proceeding with it.
    '''
    with open(devicesFile, 'r') as file:
        devices = yaml.safe_load(file) or {}

    if detid not in devices:
        fallback = devices.get(DEVICES_DEFAULT_KEY, {})
        print(f"\n\nWARNING: DETID '{detid}' is not a known device name; "
              f"falling back to '{DEVICES_DEFAULT_KEY}' entry: {fallback}")
        proceed = input('CONTINUE WITH THESE DEFAULT VALUES?  y/[N] > ').strip() or ""
        if proceed.upper() != 'Y':
            raise Exception(
                f"Please add DETID '{detid}' to {devicesFile}, or set "
                f"{'/'.join(DEVICE_KEYS)} explicitly in your config file."
            )
        return dict(fallback)

    return devices[detid]


def _require_keys(config, keys, context, extra=None):
    '''Raise KeyError if any of `keys` is missing from `config`.

    context: text describing where the keys should have come from,
             e.g. f"for testbed '...' in {_TESTBEDS_PATH} or in {userConfigFile}"
    extra:   optional additional sentence appended to the error message
    '''
    missing = [k for k in keys if k not in config]
    if missing:
        msg = f"Required key(s) {missing} not found {context}"
        if extra:
            msg += f" -- {extra}"
        raise KeyError(msg)


def _check_not_protected(key):
    '''Raise NotImplementedError if `key` is a protected FITS header key'''
    if key.upper() in PROTECTED_KEYS:
        raise NotImplementedError(f'Changing {key} is prohibited: https://tinyurl.com/DNahahah')


class Camera(_DeprecatedSquidLED):

    def __init__(self, userConfigFile):

        with open(userConfigFile, 'r') as file:
            config = yaml.safe_load(file)

        # Record who's actually running the script, as USER@HOSTNAME (not
        # user-editable -- USER is a PROTECTED_KEY)
        config['USER'] = f"{os.environ.get('USER')}@{socket.gethostname()}"

        # Check for required keys in user's config file
        _require_keys(config, YAML_REQUIRED_KEYS, f"in {userConfigFile}")

        # Badger the user to check config file
        print()
        for k,v in config.items():
            print(f'{k} = {v}' )
        print()
        configOK = input('IS YOUR CONFIG FILE CORRECT?  Y/[N] > ').strip() or ""
        if configOK.upper() != 'Y':
            msg = f'Please update your config file: {userConfigFile}'
            raise Exception(msg)

        # Fill in per-testbed defaults (HOST, PORT, ...) from testbeds.yaml,
        # keyed by TESTBED. Anything the user sets explicitly in their own
        # config file takes priority over the testbeds.yaml value.
        testbedDefaults = get_testbed_defaults(config['TESTBED'])
        config = {**testbedDefaults, **config}

        testbedContext = f"for testbed '{config['TESTBED']}' in {_TESTBEDS_PATH} or in {userConfigFile}"
        _require_keys(config, TESTBED_REQUIRED_KEYS, testbedContext)

        # LKS_HOST (Lakeshore 336) is optional -- a testbed need not have one.
        # But if LKS_HOST IS given, LKS_PORT and LKS_CHAN must be given too.
        self.configured_LKS = 'LKS_HOST' in config

        if self.configured_LKS:
            _require_keys(
                config, LKS_OPTIONAL_KEYS, testbedContext,
                extra=f"LKS_HOST is set, so {'/'.join(LKS_OPTIONAL_KEYS)} must all be given together."
            )
            config['LKS_PORT'] = int(config['LKS_PORT']) # Fix the type

        # KEYSIGHT_HOST (power supply, direct LAN control) is optional -- a
        # testbed need not have one attached. But if KEYSIGHT_HOST IS given,
        # KEYSIGHT_PORT and KEYSIGHT_CHAN must be given too.
        self.configured_KEYSIGHT = 'KEYSIGHT_HOST' in config

        if self.configured_KEYSIGHT:
            _require_keys(
                config, KEYSIGHT_OPTIONAL_KEYS, testbedContext,
                extra=f"KEYSIGHT_HOST is set, so {'/'.join(KEYSIGHT_OPTIONAL_KEYS)} must all be given together."
            )
            config['KEYSIGHT_PORT'] = int(config['KEYSIGHT_PORT']) # Fix the type
            config['KEYSIGHT_CHAN'] = int(config['KEYSIGHT_CHAN']) # Fix the type

        # Build the Keysight handle now (fixed host/port/channel for the
        # life of this Camera); None if no Keysight is configured for this
        # testbed. See KEYSIGHT_* methods below for how self.dryrun is
        # handled -- it lives entirely in Camera, not in Keysight.
        self.keysight = (
            keysight_lan.Keysight(
                config['KEYSIGHT_HOST'], config['KEYSIGHT_PORT'], config['KEYSIGHT_CHAN']
            )
            if self.configured_KEYSIGHT else None
        )

        # Fill in per-device bias defaults (VEXTRAHI, VEXTRALO, ...) from
        # devices.yaml, keyed by DETID. Falls back to devices.yaml's DEFAULT
        # entry (with a warning) if DETID isn't listed there. Anything the
        # user sets explicitly in their own config file still takes priority.
        deviceDefaults = get_device_defaults(config['DETID'])
        config = {**deviceDefaults, **config}

        # Normalize types in-place (YAML may hand these back as strings
        # depending on how they're quoted) 
        config['VEXTRAHI'] = float(config['VEXTRAHI'])
        config['VEXTRALO'] = float(config['VEXTRALO'])

        # config is now fully assembled, validated, and type-coerced --
        # save it as-is, with no further changes to follow
        self.userConfigFile = userConfigFile
        self.config = config

        # Reset internal counters; These are useful to predict test length with dryrun=True
        self.dryrun = False
        self.timetotal = 0  # Estimate of total time (s) spent on commands
        self.filetotal = 0  # Number of times expose() has been called
        self.frametotal = 0 # Total number of frames taken via expose() (nexp+1 per call)
                            
        # Test connection to SQUID server
        assert self.ping()  # Returns True if connected
        print('connected')

        # Remove all FITS headers and set required headers from config
        self.FITSkey_clear()

        # Naming convention
        self.send('set name_format standard')  # standard "basename_0000" | procedure "DATETIME_filename_0000"

        # Set output directory (below server home) to ../DEVICE/TESTBED/DATE
        # We don't provide this as a helper function - we don't want people setting it arbitrarily
        subdir = '/'.join( [config['DETID'], config['TESTBED'], datetime.now().strftime("%y%m%d")] ) # YYMMDD
        # self.send(f'subdir {subdir}')

        # Lakeshore 336 is optional -- warn if none is configured for this testbed,
        # otherwise do a one-shot test read so connection problems show up now
        # Failure here disables self.configured_LKS for the rest of the session
        if not self.configured_LKS:
            print('WARNING: no Lakeshore 336 configured for this testbed '
                  '(LKS_HOST not set) -- TEMPDET will not be recorded')
        else:
            T = self._read_TEMPDET()
            if T is not None:
                print(f"Lakeshore 336 channel {self.config['LKS_CHAN']} current temperature: {T:.3f} K")
            else:
                self.configured_LKS = False

        # Keysight power supply is optional -- warn if none is configured, 
        # otherwise do a one-shot test read so connection problems show up now 
        # Failure here disables self.configured_KEYSIGHT for the rest of the session.
        if not self.configured_KEYSIGHT:
            print('WARNING: no Keysight power supply configured for this testbed '
                  '(KEYSIGHT_HOST not set) -- KEYSIGHT_* methods will be unavailable')
        else:
            try:
                self.keysight.state()
            except Exception as e:
                print(f'WARNING: could not reach Keysight power supply: {e}')
                self.configured_KEYSIGHT = False


    def _read_TEMPDET(self):
        '''Read the current temperature (K) from the testbed's Lakeshore 336

        Returns the temperature as a float, or None if no Lakeshore is
        configured (self.configured_LKS is False) or if the read fails
        (a warning is printed in that case).
        '''
        if not self.configured_LKS:
            return None

        try:
            return read_lakeshore336_temperature(
                self.config['LKS_HOST'], self.config['LKS_PORT'],
                self.config['LKS_CHAN'], retries=LKS_RETRIES
            )
        except Exception as e:
            print(f'WARNING: could not read Lakeshore 336 temperature: {e}')
            return None

    def send(self, cmd, **kwargs):
        '''Workhorse method based on the static method below''' 
        if self.dryrun:
            print(cmd)
            return ['']

        return Camera.send_static(cmd, host=self.config['HOST'], port=self.config['PORT'], **kwargs)

    @staticmethod
    def send_static(cmd, host, port, parse=True, timeout=None, quiet=False):
        '''Send a server command as a text string and return the server response
        Response will be parsed into a space-delimited list unless parse=False
        '''

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # ^ Context manager closes the socket if there's an error

            s.settimeout(TO_DEFAULT)
            try:
                s.connect((host, port))
            except TimeoutError as e:
                print('\nCONNECTION TO SERVER TIMED OUT\n')
                raise(e)

            s.settimeout(None)  ### Commands should have their own timeout  ## could just override here

            if not quiet: print(f"Sending: {cmd}")
            command = cmd+'\n'
            s.sendall(command.encode())
            response = s.recv(1024).decode()
            response_split = response.split()  # Delimit by spaces
            status = response_split[0]  # First word before space should be OK or ERROR

            if status.upper() != "OK":
                raise RuntimeError(response)

            detail = response_split[1:] if len(response_split) > 1 else None

            if parse: return detail
            return response

    def send_MISC(self, cmd, **kwargs):
        '''Send a native MISC command.  Same options as send() '''
        return self.send('misc '+cmd)

    def _send_with_progress(self, cmd, predicted, **kwargs):
        '''Same as send(), but displays a progress bar in the foreground
        while a slow blocking command runs in a background thread.

        predicted:  Expected duration (s), used only to pace the bar.
                    The actual wait always ends whenever the server
                    responds -- sooner or later than predicted, not when
                    the bar reaches 100%.

        This relies on socket.recv() releasing the GIL while it blocks,
        so the foreground loop keeps printing even though send() itself
        is single-threaded.
        '''
        if self.dryrun:
            return self.send(cmd, **kwargs)

        result = {}
        def worker():
            try:
                result['value'] = self.send(cmd, **kwargs)
            except Exception as e:
                result['error'] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        start = time.time()

        # Give the worker thread a moment to print send()'s own "Sending: ..."
        # line first, so it doesn't land in the middle of the progress bar
        time.sleep(0.5)  #0.15 works

        bar_len = 30
        try:
            while thread.is_alive():
                elapsed = time.time() - start
                frac = min(elapsed/predicted, 1.0) if predicted > 0 else 1.0
                filled = int(bar_len*frac)
                bar = '#'*filled + '-'*(bar_len-filled)
                print(f'\r  [{bar}] {elapsed:5.1f}/{predicted:.1f}s ({100*frac:3.0f}%)',
                      end='', flush=True)
                thread.join(timeout=0.5)
        except KeyboardInterrupt:
            print('\n  CTRL-C: no longer displaying progress, but still waiting for the '
                  'server to finish (the exposure continues on hardware regardless) -- '
                  'CTRL-C again to abort the script.')
            thread.join()

        elapsed = time.time() - start
        print(f'\r  [{"#"*bar_len}] {elapsed:5.1f}/{predicted:.1f}s (100%)  done')

        if 'error' in result:
            raise result['error']
        return result.get('value')

    def ping(self):
        '''Ping the server; return True if server returns "PONG" '''
        response = self.send('ping')[0]
        print(response)
        return (response.upper())=='PONG'

    def _load(self):
        '''This resets the board, loads the firmware, and leaves the MISC idle.  Follow with init()'''
        return self.send('load')

    def _init(self):
        '''This starts the MISC running.  Multiple calls after load() may crash the system.'''
        return self.send('init')

    def sleep(self, delay):
        '''Sleep for `delay` seconds, printing time elapsed as it progresses.

        Gracefully handles KeyboardInterrupt (CTRL-C skips the remaining wait).
        Returns the actual elapsed time (s), regardless if sleep was completed or interrupted.
        '''
        print(f'Waiting {delay} sec...  (CTRL-C to skip)')
        elapsed = delay
        if not self.dryrun:
            start = time.time()
            step = 5
            remaining = delay
            try:
                while remaining > 0:
                    chunk = min(step, remaining)
                    time.sleep(chunk)
                    remaining -= chunk
                    print(f'\r  ...{time.time() - start:.0f}/{delay} sec', end='', flush=True)
            except KeyboardInterrupt:
                elapsed = time.time() - start
                print(f'\nSleep interrupted by user after {elapsed:.2f} sec')
            else:
                print()
        print('Done!')

        return elapsed

    def restartBBX(self, settle=10):
        '''Combination of _load() and _init().  Avoid using these separately.

        settle:  Wait time (s) after reset before continuing.
        This can be cleanly interrupted with CTRL-C.
        '''
        _ = self._load()
        print(_)
        _ = self._init()
        print(_)

        # Settle, while displaying time elapsed
        print('Settling after BBX reset...')
        elapsed = self.sleep(settle)

        self.FITSkey('TIMSETTL', round(elapsed))
        self.timetotal += elapsed + TT_RESTART_S

        return _

    def filebase(self, setval: str | None=None):
        '''Get or set the file basename'''
        if setval is not None:
            return self.send(f'set filebase {setval}')[0]
        else:
            return self.send('get filebase')[0]

    basename = filebase  # familiar alias

    def expIndex(self, setval: int | None=None):
        '''Get or set the image number for filenaming'''
        if setval is not None:
            return self.send(f'set exposureIndex {setval}')[0]
        else:
            return int(self.send('get exposureIndex')[0])

    imnum = expIndex  # familiar alias

    def FITSkey(self, key: str, setval: str | None=None):
        '''Get or set a FITS header
        Throws RuntimeError when key doesn't exist
        '''
        if setval is not None:
            _check_not_protected(key)
            return self.send(f'fits_set {key} {setval}')
        else:
            return self.send(f'fits_get {key}')[0]

    def FITSkeys(self, keys: dict):
        '''Set multiple FITS headers from a dictionary of key/value pairs
        Throws NotImplementedError if any key is protected
        '''
        for key in keys:
            _check_not_protected(key)

        for key, setval in keys.items():
            self.FITSkey(key, setval)

    def FITSkey_clear(self, key: str | None=None):
        '''Clear user-defined FITS headers'''
        if key is not None:
            _check_not_protected(key)
            return self.send(f'fits_clear {key}')
        else:
            print( self.send(f'fits_clear_all')[0] )

            # Replace required FITS headers
            print('Setting required FITS headers')

            # Circumvent FITSkey(), set all protected FITS headers
            for k,v in self.config.items(): self.send(f'fits_set {k} {v}')  

    def FITSkey_list(self):
        '''List all user-defined FITS header keys/values'''
        return self.send(f'fits_list')


    def exptime(self, setval: float | None=None):
        '''Get or set the default exposure time in seconds.
        Exposures use this value if no exptime is passed to expose()
        '''
        if setval is not None:
            return self.send(f'set exptime {setval}')[0]
        else:
            return float(self.send('get exptime')[0])

    def expose(self, exptime: float, nexp: int=1):
        '''Start an exposure series; raw image data will land in project data directory
        exptime = Exposure time (s)
        nexp = Number of exposures
        '''

        # dt = self.exptime() if exptime is None else exptime # Never used?
        predicted = exptime*nexp + overhead(nexp) # estimated time (s) for send() to return
        self.timetotal += predicted # update timing estimate
        self.filetotal += 1         # count calls to expose()
        self.frametotal += nexp+1   # count frames taken (nexp + 1)

        # Record detector/cryostat temperature from the Lakeshore 336, if one
        # is configured and reachable.
        T = self._read_TEMPDET()
        self.FITSkey('TEMPDET', T if T is not None else '')

        print(f'Exposing: {nexp} x {exptime}s  (wait ~~{predicted:.1f}s)')
        return self._send_with_progress(f'multi_expose {exptime} {nexp}', predicted)

    def expose_STIME(self, exptime: float, nexp: int=1):
        '''Same as expose() but the server controls exposure timing instead of camera electronics'''

        # dt = self.exptime() if exptime is None else exptime
        predicted = exptime*nexp + overhead(nexp) # estimated time (s) for send() to return
        self.timetotal += predicted # update timing estimate
        self.filetotal += 1         # count calls to expose_STIME()
        self.frametotal += nexp+1   # count frames taken (nexp exposures + 1 baseline/reference frame)

        print(f'Exposing: {nexp} x {exptime}s  (predicted wait ~{predicted:.1f}s)')
        return self._send_with_progress(f'expose {exptime} {nexp}', predicted)

    def set_gain(self, gain: str, TG: bool=True):
        ''' Set detector gain mode; optionally enable/disable transfer gate'''
        OKgains = ['high','hi','low','lo','dual']
        gainNorm = gain.lower().strip()
        if gainNorm not in OKgains:
            raise ValueError('Invalid gain mode: '+gain)

        # Set appropriate V_EXTRA for gain mode
        v_extra = self.config['VEXTRALO'] if gainNorm.startswith('lo') else self.config['VEXTRAHI']
        _ = self.set_bias('V_EXTRA', v_extra)

        # Set gain mode
        TGstr = 'true' if TG else 'false'
        ret = self.send(f'set_hardware {gain} {TGstr}')
        return  ret

    def set_NOGLO(self, ng: int):
        ''' Change detector controller NOGLO mode;  ints 0-15; 0-7 all bad ### 14 is best (TBC)
        8: off
        9: Vhighs off
        10: I_PIX_PAD=3.3V
        11: Vhighs off + I_PIX_PAD=3.3V
        12-15: Same order as above + YBLK_EN’s off 
        '''
        return self.send(f'set_noglo {ng}')

    def set_bias(self, bias: str, volts: float):

        OKbiases = ['PIX_REF', 'V_EXTRA', 'PIX_SUPPLY', 'VHIGH_TG', 'VLOW_TG']
        if bias.upper().strip() not in OKbiases:
            raise ValueError('Invalid bias name: '+bias)

        if (volts<VMIN) or (volts>VMAX):
            raise ValueError(f'Bias out of range: {volts}   range=[{VMIN},{VMAX}]')

        return self.send(f'set_bias {bias} {volts}')

    def set_biases(self, biases: dict):
        ''' Assumes dict of the form {'BIAS1':v1, 'BIAS2':v2, ...} '''
        ret = []
        for k,v in biases.items(): 
            ret.append( self.set_bias(k,v) )
        return ret

    # --- BBx LED control
    # If the LED is routed through BBx, it is toggled via MISC commands
    # Power settings are still controlled via a Keysight supply
    # Both the MISC switch and the Keysight power must be ON to light the LED

    def LED_MISC_ON(self):
        '''Turn on the MISC's LED switch'''
        _ = self.send('misc_led_on')
        self.FITSkey('LEDMISC',True)
        return self._LED_state_squid()

    def LED_MISC_OFF(self):
        '''Turn off the MISC's LED switch'''
        _ = self.send('misc_led_off')
        self.FITSkey('LEDMISC',False)
        self.FITSkey('LEDON',False)
        return _

    # --- Keysight power supply (direct LAN control)
    # Operate the LED over VISA/VXI-11, through self.keysight configured via testbeds.yaml
    # The channel is fixed on self.keysight, so it's not a parameter here.

    def _check_KEYSIGHT(self):
        '''Raise RuntimeError if no Keysight power supply is configured for this testbed'''
        if not self.configured_KEYSIGHT:
            raise RuntimeError(
                'No Keysight power supply configured for this testbed '
                '(KEYSIGHT_HOST not set in testbeds.yaml or your config file).'
            )

    def _KEYSIGHT_guard(self, **kwargs):
        '''Check that a Keysight is configured (raises if not), and print
        the pending call (caller's own method name + kwargs).

        Returns self.dryrun
        '''
        self._check_KEYSIGHT()
        method_name = sys._getframe(1).f_code.co_name  # the caller's own name
        arg_str = ', '.join(f'{k}={v}' for k, v in kwargs.items())
        print(f'{method_name}({arg_str})')
        return self.dryrun

    def LED_state(self):
        '''Read voltage/current/output-state from the Keysight power
        supply and update stored FITS headers.

        Von:  bool; True if power supply channel is on
        Vset: float; Programmed voltage setting (V)
        Vmeas: float; Voltage measured by Keysight (V)
        '''
        if self._KEYSIGHT_guard():
            return

        Von, Vset, Vmeas  = self.keysight.state()

        LEDon = (Von and Vset>0 and Vmeas>0)

        self.FITSkey('LEDV', Vset)
        self.FITSkey('LEDPWR', Von)
        self.FITSkey('LEDON', LEDon)

        return Von, Vset, Vmeas

    def LED_ON(self):
        '''Turn on the LED power'''
        if self._KEYSIGHT_guard():
            return
        _ = self.keysight.output_on()
        return self.LED_state()

    def LED_OFF(self):
        '''Turn off the LED power'''
        if self._KEYSIGHT_guard():
            return
        _ = self.keysight.output_off()
        return self.LED_state()

    def LED_V(self, setval: float):
        '''Set the LED voltage (V)
        Function returns a tuple (V_on, V_set, V_measured) '''
        if self._KEYSIGHT_guard(setval=setval):
            return
        _ = self.keysight.set_voltage(setval)
        return self.LED_state()

    def expose_with_flash(self, exptime, volts, delay_off, delay_on=NICARD_DELAY+SCANTIME_S+MARGIN_S+1):
        ''' Do 1 exposure with a timed LED flash, triggered directly on the
        Keysight power supply over LAN (see keysight_lan.Keysight.flash_LED).

        exptime = [s] Extra integration time of the exposure (not including scan time)
        volts   = [V] LED voltage
        delay_on = [s] How long to wait before LED turns on
        delay_off = [s] LED flash duration

        The default delay_on should avoid the flash overlapping the baseline scan.
        Exptime should be long enough to avoid the flash overlapping the 2nd scan.
        '''
        # Min start time: NICARD_DELAY + SCANTIME_S
        # Max end time: NICARD_DELAY + SCANTIME_S + exptime
        DELAY_ON = NICARD_DELAY+SCANTIME_S

        assert delay_on > DELAY_ON + MARGIN_S  # Don't start flash before 1st scan is done
        assert delay_on + delay_off < DELAY_ON - MARGIN_S + exptime  # Finish flash before 2nd scan

        self.FITSkey('FLASH',True)
        self.FITSkey('FLASHV',volts)
        self.FITSkey('FLASHT',delay_off)
        print(f'Flashing {volts}V for {delay_off}s')

        if not self._KEYSIGHT_guard(delay_on=delay_on, delay_off=delay_off, volt=volts):
            self.keysight.flash_LED(delay_on, delay_off, volts)  # Keysight does not block for this command

        _ = self.expose(exptime)

        self.FITSkey('FLASH',False)
        dum = self.LED_state()
        return _


def SQUID_logo():
    logo = "   _____  ____   __  __ ____ ____  \n"
    logo+= "  / ___/ / __ \\ / / / //  _// __ \\ \n"
    logo+= "  \\__ \\ / / / // / / / / / / / / / \n"
    logo+= " ___/ // /_/ // /_/ /_/ / / /_/ /  \n"
    logo+= "/____/ \\___\\_\\\\____//___//_____/   \n"
    logo+= "Server for Quick UVEX Image Data  <コ:彡 \n"
    print(logo)

if __name__ == "__main__":
    ''' MAIN script exposes the camera server interface for debugging.
    Use it to send server command strings.
    '''

    if len(sys.argv) < 3:
        sys.exit('Usage:  camera_cmd.py HOST:PORT COMMAND WORDS')

    host, _, port = sys.argv[1].partition(':')
    if not port:
        sys.exit('Usage:  camera_cmd.py HOST:PORT COMMAND WORDS')
    port = int(port)

    cmd = ' '.join(sys.argv[2:])  # Concat remaining args into 1 string

    _ = Camera.send_static(cmd, host=host, port=port)
    print(_)

