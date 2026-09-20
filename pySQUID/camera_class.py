#!/disk/bifrost/uvexdet/miniconda3/bin/python
'''
Class for sending commands to the camera server
'''

# TODO: Print/recover default bias settings
# Get safe bias ranges
# Fix timeouts -- have some short default for all commands except exposures
# UNTESTED: set_hardware_window <start row> <end row>

from datetime import datetime
import os
import socket
import sys
import time
import yaml
 
TO_DEFAULT = 3 # Default timeout (s) for server connections and commands

# Safety limits; ### TBC
VMIN, VMAX = (0,3.3)

### Detector-dependent settings - should be in config file
V_EXTRA_HI = 3.011
V_EXTRA_LO = 3.177


NICARD_DELAY = 2.   # Delay between sending "expose" and start of 1st frame scan
SCANTIME_S   = 8.5  # Aproximate FULL-FRAME scan time ### LOW GAIN 6s
MARGIN_S     = 1.
FLASH_DELAY_S = NICARD_DELAY+SCANTIME_S+MARGIN_S # Minimum delay before flashing LED

# Can't proceed unless these exist in user's config file
YAML_REQUIRED_KEYS = ['OPERATOR', 'TESTBED', 'DETID', 'DETTYPE', 'DETCTRL', 'LEDWAVE', 'HOST', 'PORT']

PROTECTED_KEYS = ['USERNAME']
PROTECTED_KEYS += YAML_REQUIRED_KEYS


class Camera:

    def __init__(self, userConfigFile):

        with open(userConfigFile, 'r') as file:
            config = yaml.safe_load(file)
            
        # Check for required keys in user's config file
        for k in YAML_REQUIRED_KEYS:
            if k not in config.keys():
                raise KeyError(f'Required key {k} not found in {userConfigFile}')

        # Badger the user to check config file
        print()
        for k,v in config.items():
            print(f'{k} = {v}' )
        print()
        configOK = input('IS YOUR CONFIG FILE CORRECT?  Y/[N] > ').strip() or ""
        if configOK.upper() != 'Y':
            msg = f'Please update your config file: {userConfigFile}'
            raise Exception(msg)

        self.host = config['HOST']
        self.port = config['PORT']
        self.dryrun = False

        assert self.ping()  # Returns True if connected
        print('connected')

        self.userConfigFile = userConfigFile
        self.config = config

        # Remove all FITS headers and set required headers from config
        self.FITSkey_clear()

        # Naming convention
        self.send('set name_format standard')  # standard "basename_0000" | procedure "DATETIME_filename_0000"


    def send(self, cmd, **kwargs):
        '''Workhorse method based on the static method below''' 
        if self.dryrun:
            print(cmd)
            return ['']

        return Camera.send_static(cmd, host=self.host, port=self.port, **kwargs)

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

    def restartBBX(self, settle=0):
        '''Combination of _load() and _init().  Avoid using these separately.

        settle:  Wait time (s) after reset before continuing
        '''
        _ = self._load()
        print(_)
        _ = self._init()
        print(_)
        print(f'Settling after BBX reset, waiting {settle} sec...')
        if not self.dryrun: time.sleep(settle)
        print('Done!')
        self.FITSkey('TIMSETTL', settle)

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
            if key.upper() in PROTECTED_KEYS:
                raise NotImplementedError(f'Changing {key} is prohibited: https://tinyurl.com/DNahahah')
            return self.send(f'fits_set {key} {setval}')
        else:
            return self.send(f'fits_get {key}')[0]

    def FITSkey_clear(self, key: str | None=None):
        '''Clear user-defined FITS headers'''
        if key is not None:
            if key.upper() in PROTECTED_KEYS:
                raise NotImplementedError(f'Changing {key} is prohibited: https://tinyurl.com/DNahahah')
            return self.send(f'fits_clear {key}')
        else:
            print( self.send(f'fits_clear_all')[0] )

            # Replace required FITS headers
            print('Setting required FITS headers')

            DATE = datetime.today().strftime('%Y%m%d')
            self.config['OUTDIR'] = f"{self.config['DETID']}/{DATE}/"
            self.config['USERNAME'] = os.getlogin()
            
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
        return self.send(f'multi_expose {exptime} {nexp}')

    def expose_STIME(self, exptime: float, nexp: int=1):
        '''Same as expose() but the server controls exposure timing instead of camera electronics''' 
        return self.send(f'expose {exptime} {nexp}')

    def set_gain(self, gain: str, TG: bool=True):
        ''' Set detector gain mode; optionally enable/disable transfer gate'''
        OKgains = ['high','hi','low','lo','dual']
        if gain.lower().strip() not in OKgains:
            raise ValueError('Invalid gain mode: '+gain)

        # Set appropriate V_EXTRA for gain mode
        v_extra = V_EXTRA_LO if gain.lower().strip().startswith('lo') else V_EXTRA_HI
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

    def LED_state(self):
        ''' Query LED state and update stored FITS headers '''

        # Example response from Keysight script
        # 1> 14:54:06  set  3.000 V  ON   meas  3.000 V  0.052 mA\r\n
        response = self.send('led_read', parse=False)

        if self.dryrun: return response

        Von = response.split('meas')[0].split()[-1]  # item before "meas"
        Vset = response.split('set')[1].split()[0]    # item after "set"
        Vmeas = response.split('meas')[1].split()[0]  # item after "meas"

        Von = Von.upper()=='ON'
        Vset = float(Vset)
        Vmeas = float(Vmeas)
        LEDon = (Von and Vset>0 and Vmeas>0)

        self.FITSkey('LEDV', Vset)
        self.FITSkey('LEDPWR', Von)
        self.FITSkey('LEDON', LEDon)

        return Von, Vset, Vmeas  # bool, float, float

    def LED_MISC_ON(self):
        '''Turn on the MISC's LED switch'''
        _ = self.send('misc_led_on')
        self.FITSkey('LEDMISC',True)
        return self.LED_state()

    def LED_MISC_OFF(self):
        '''Turn off the MISC's LED switch'''
        _ = self.send('misc_led_off')
        self.FITSkey('LEDMISC',False)
        self.FITSkey('LEDON',False)
        return _

    def LED_ON(self):
        '''Turn on the LED power'''
        _ = self.send('led_on')
        self.FITSkey('LEDPWR',True)
        return self.LED_state()

    def LED_OFF(self):
        '''Turn off the LED power'''
        _ = self.send('led_off')
        self.FITSkey('LEDPWR',False)
        self.FITSkey('LEDON',False)
        return _

    def LED_V(self, setval: float):
        '''Set the LED voltage (V)
        Function returns a tuple (V_on, V_set, V_measured) '''
        _ = self.send(f'set_led_voltage {setval}')
        return self.LED_state()

    def _LED_flash(self, delay_on: float, delay_off: float, volts: float):
        ''' After <delay_on>, flash the LED at <volts> for <delay_off>
        Time measured in seconds '''
        return self.send(f'led_flash {delay_on} {delay_off} {volts}')

    def expose_with_flash(self, exptime, volts, delay_on, delay_off):
        ''' Do 1 exposure with a timed LED flash.'''
        # Min start time: FLASH_DELAY_S
        # Max end time: FLASH_DELAY_S + nexp*(exptime + SCANTIME_S)

        assert delay_on > FLASH_DELAY_S  # Don't start before 1st scan is done
        assert delay_on + delay_off < FLASH_DELAY_S + exptime  # Finish before 2nd scan

        self.FITSkey('FLASH',True)
        self.FITSkey('FLASHV',volts)
        self.FITSkey('FLASHT',delay_off)
        print(f'Flashing {volts}V for {delay_off}s')

        self._LED_flash(delay_on, delay_off, volts)
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

