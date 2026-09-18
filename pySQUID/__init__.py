"""
pySQUID
=======

Control and data-reduction tools for the UVEX SQUID camera testbed.

camera_class is the main part of this package (camera control; only needs
PyYAML, already a hard dependency) so it's imported eagerly here, along with
its Camera and SQUID_logo names for convenience::

    import pySQUID
    cam = pySQUID.Camera('config.yaml')
    pySQUID.SQUID_logo()

The data-reduction submodules pull in heavier, separate dependencies
(astropy, nptdms) and are NOT imported here — import them directly when
needed::

    from pySQUID import tdms_to_fits
    from pySQUID import cdssubtract_BBx
"""

__version__ = "0.1.0"

from . import camera_class
from .camera_class import Camera, SQUID_logo
