# Example pySQUID script to acquire image lag data
#
# USAGE:  python lag_example.py

# PROTIP: Run long scripts in 'screen' to avoid shutdown
#		https://www.geeksforgeeks.org/linux-unix/screen-command-in-linux-with-examples/ 

import numpy as np
import sys
import time
from pySQUID import camera_class  # Camera class for talking to the SQUID testbed server

DRYRUN = True  	# DRYRUN=True means just print the commands, don't execute them

USERCONFIG = '/disk/bifrost/uvexdet/pySQUID/pySQUID/USER.yaml'  # User's config file
FILEBASE = 'lagtime0'  # Test name for filenames
I_START = 0  	# Starting filename tag number --> lagtime0_0000

TIMSETTL = 600  # Wait time (s) after BBX reset to allow settling (use for precision measurements)
				# Unclear what this value should be - it is based the older Archon controller and VIB
				# And we should probably standardize this

# Extra FITS headers not provided automatically
FITS_HEADERS = {
	'TEMPTEST': 172.,  # Nominal detector temperature (K) for this test
	#'KEYWORD': VALUE, # Description
}

### This shouldn't be necessary - we should have fixed default settings and know what they are
BIASES = {
	'VHIGH_TG':2.0
	}

NOGLO = 14  # We think the best NOGLO mode is typically 14

# These settings produce ~2x saturation in low gain mode
# Include 0V as baseline measurement with same timing
VLED = (0, 6.0)
EXPTIME_FLASH_S = 45
DELAY_FLASH_S = 12
FLASH_S = 24

NEXP_DARK = 12  # Number of exposures after LED flash
DARKTIME_S = 300  # Dark duration (s)
# DARKTIMES_S = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
# np.random.shuffle(DARKTIMES_S)  # Randomize to disrupt trends

#------- END OF HARDCODED PARAMETERS -------#

# Estimate run time
scantime = camera_class.SCANTIME_S
t_estimate = DARKTIME_S*NEXP_DARK + scantime*(NEXP_DARK+1) + (EXPTIME_FLASH_S+2*scantime)
t_estimate *= len(VLED)
t_estimate += TIMSETTL
t_estimate /= 3600.
print(f'Estimated run time:  {round(t_estimate,1)} hours')

#------- START TEST -------#
t0 = time.time()

# Setup camera
try:
	cam = camera_class.Camera(USERCONFIG)  # Connect to the testbed server; cam is used for all camera_class calls below
except Exception as e:
	print(e)
	sys.exit(1)

cam.dryrun = DRYRUN  # If True, print commands instead of executing them

cam.restartBBX(settle=TIMSETTL)  # Make sure BBX is in our default configuration

for k, v in FITS_HEADERS.items(): cam.FITSkey(k,v)  # Load custom FITS headers

cam.filebase(FILEBASE)  # Set the output FITS filename base
cam.imnum(I_START)      # Set the starting image number for filenaming

cam.set_biases(BIASES)  ### Set bias voltages to non-defaults
cam.set_NOGLO(NOGLO)    # Set detector controller NOGLO mode

cam.LED_MISC_ON()  # Enable MISC LED switch
cam.LED_OFF()      # Start with LED power off

# Clear detector
cam.set_gain('HIGH')              # Switch detector to high gain mode
print( cam.expose(0,NEXP_DARK) )  # Take NEXP_DARK zero-second clearing exposures

# Loop over parameter lists and acquire images
for vled in VLED:

	# Single exposure with flash # exptime, volts, delay_on, Flash duration
	cam.set_gain('LOW')                                                       # Switch detector to low gain mode
	_ = cam.expose_with_flash(EXPTIME_FLASH_S, vled, DELAY_FLASH_S, FLASH_S)  # Expose with a timed LED flash
	print(_)

	# Darks to watch lag decay;  0th image will contain ~1 frame time of lag
	cam.set_gain('HIGH')           # Back to high gain mode for the dark series
	_ = cam.expose(DARKTIME_S, NEXP_DARK)  # Take NEXP_DARK dark exposures of length dt
	print(_)

cam.FITSkey_clear()  # Clear user-defined FITS headers
print('DONE!\n')

t1 = time.time()
t_actual = (t1-t0)/3600.
print(f'Estimated run time:  {round(t_estimate,2)} hours')
print(f'Actual run time:     {round(t_actual,2)} hours')
