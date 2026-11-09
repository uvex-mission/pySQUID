# Example pySQUID script to check which NOGLO setting is best
#
# USAGE:  python darktest.py

# PROTIP: Run long scripts in 'screen' to avoid accidental shutdown
#		https://www.geeksforgeeks.org/linux-unix/screen-command-in-linux-with-examples/ 

import numpy as np
import sys
import time
from pySQUID import camera_class  # Camera class for talking to the SQUID testbed server

DRYRUN = input('Is this a DRYRUN?  [Y]/N:  ').upper()!='N'  # DRYRUN just prints commands without executing

USERCONFIG = '/disk/bifrost/uvexdet/pySQUID/pySQUID/USER.yaml'  # User's config file
FILEBASE = 'darktest'  # Test name for filenames
# I_START = 0  	# Starting filename tag number ; skip to guard against filename collisions

TIMSETTL = 600  # Wait time (s) after BBX reset to allow settling (use for precision measurements)
				# Unclear what this value should be - it is based the older Archon controller and VIB
				# And we should probably standardize this

# Extra FITS headers not provided automatically
FITS_HEADERS = {
	'TEMPTEST': 172.,  # Nominal detector temperature (K) for this test
	#'KEYWORD': VALUE, # Description
}

NOGLO = range(8,16) 	# The best NOGLO mode is typically 14

NEXP_INIT = 10          # Initial darks to clear detector
NEXP      = 3   		# Number of exposures per NOGLO
EXPDELAY  = 92  		# Exposure time (added to scan time)
GAIN = 'HIGH'

#------- END OF HARDCODED PARAMETERS -------#

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

cam.FITSkeys(FITS_HEADERS)  # Load custom FITS headers
cam.filebase(FILEBASE)  	# Set the output FITS filename base
# cam.imnum(I_START)      	# Set the starting image number for filenaming

# cam.set_biases(BIASES)  	# Set bias voltages to non-defaults
cam.LED_MISC_OFF()  		# Disable MISC LED switch

# Clear detector
cam.set_gain('HIGH')              # Switch detector to high gain mode
print( cam.expose(0,NEXP_INIT) )  # Take NEXP_INIT zero-second clearing exposures

cam.set_gain(GAIN)

# Loop over parameter lists and acquire images
for ng in NOGLO:

	cam.set_NOGLO(ng)    	# Set detector controller NOGLO mode
	_ = cam.expose(EXPDELAY, NEXP)  # Take NEXP dark exposures
	print(_)

cam.FITSkey_clear()  # Clear user-defined FITS headers
print('DONE!\n')

# Summarize test timing
t1 = time.time()
t_actual = (t1-t0)
print(f'Estimated run time:  {round(cam.timetotal/3600,2)} hours')
print(f'Actual run time:     {round(t_actual/3600,2)} hours')
