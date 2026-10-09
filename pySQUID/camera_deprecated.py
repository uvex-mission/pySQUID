'''
Deprecated LED control methods that drive the Keysight indirectly via the
SQUID server's own 'led_*'/'misc_led_*' commands, instead of talking to the
Keysight directly over LAN (see keysight_lan.Keysight / Camera.LED_* in
camera_class.py). Kept here for reference/fallback -- mixed into Camera
via _DeprecatedSquidLED so these still work as self._LED_state_squid(),
etc., with no change at any call site.
'''

from .camera_class import NICARD_DELAY, SCANTIME_S, MARGIN_S, TT_LEDSTATE_S


class _DeprecatedSquidLED:

    def _LED_state_squid(self):
        '''[DEPRECATED] Query LED state via the SQUID server's own
        'led_read' command and update stored FITS headers. Superseded by
        LED_state()'''

        self.timetotal += TT_LEDSTATE_S

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

    def _LED_ON_squid(self):
        '''[DEPRECATED] Turn on the LED power via the SQUID server's own
        'led_on' command. Superseded by LED_ON()'''
        _ = self.send('led_on')
        self.FITSkey('LEDPWR',True)
        return self._LED_state_squid()

    def _LED_OFF_squid(self):
        '''[DEPRECATED] Turn off the LED power via the SQUID server's own
        'led_off' command. Superseded by LED_OFF()'''
        _ = self.send('led_off')
        self.FITSkey('LEDPWR',False)
        self.FITSkey('LEDON',False)
        return _

    def _LED_V_squid(self, setval: float):
        '''[DEPRECATED] Set the LED voltage (V) via the SQUID server's own
        'set_led_voltage' command. Superseded by LED_V().
        Function returns a tuple (V_on, V_set, V_measured) '''
        _ = self.send(f'set_led_voltage {setval}')
        return self._LED_state_squid()

    def _LED_flash(self, delay_on: float, delay_off: float, volts: float):
        ''' After <delay_on>, flash the LED at <volts> for <delay_off>
        Time measured in seconds '''
        return self.send(f'led_flash {delay_on} {delay_off} {volts}')

    def _expose_with_flash_squid(self, exptime, volts, delay_off, delay_on=NICARD_DELAY+SCANTIME_S+MARGIN_S+1):
        '''[DEPRECATED] Do 1 exposure with a timed LED flash via the SQUID
        server's own 'led_flash' command. Superseded by expose_with_flash().

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

        self._LED_flash(delay_on, delay_off, volts) # SQUID does not block for this command
        _ = self.expose(exptime)

        self.FITSkey('FLASH',False)
        dum = self._LED_state_squid()
        return _
