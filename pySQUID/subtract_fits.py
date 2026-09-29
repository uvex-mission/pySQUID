#!/usr/bin/env python
# subtract_fits.py
# Author: Chaz Shapiro
#
# CLI wrapper for pySQUID.FITStools.diff_hdulists(): subtract one FITS file
# from another, extension-wise (file1 - file2), and write the result to a
# new FITS file whose name is file1's basename with a tag prepended.
#
# Usage:
#   subtract_fits file1.fits file2.fits [options]
#
# Examples:
#   subtract_fits run01.fits run02.fits            -> writes dsub_run01.fits
#   subtract_fits run01.fits run02.fits -tag diff  -> writes diff_run01.fits
#   subtract_fits run01.fits run02.fits -keys DETID GAINMODE
#
# Options:
#   -tag TAG      Prepended (with an underscore) to file1's basename to make
#                 the output filename, e.g. tag_file1.fits; default='dsub'.
#                 The output is written in the same directory as file1,
#                 always overwriting any existing file at that path.
#   -keys KEY [KEY ...]
#                 FITS header keyword(s) that must match exactly,
#                 extension-wise, between the two files, in addition to the
#                 data-shape check FITStools.diff_hdulists() always applies.
#                 The whole diff fails if even one extension mismatches.
#   -verbose      Print the output filename after writing.

import argparse
import os
import sys

import astropy.io.fits as pf

from pySQUID import FITStools as tools


def diff_files(file1, file2, tag='dsub', keys=None):
    '''
    Diff two FITS files extension-wise (file1 - file2) via
    FITStools.diff_hdulists(), and write the result to a new file named
    "<tag>_<basename of file1>" in the same directory as file1. Any
    existing file at that path is overwritten.

    Returns the output path.
    '''
    hdulist1 = pf.open(file1, mode='readonly')
    hdulist2 = pf.open(file2, mode='readonly')

    try:
        diffed = tools.diff_hdulists(hdulist1, hdulist2, keys=keys)
    finally:
        hdulist1.close()
        hdulist2.close()

    inpath, basename = os.path.split(file1)
    if inpath == '':
        inpath = '.'
    outpath = os.path.join(inpath, f'{tag}_{basename}')

    diffed.writeto(outpath, overwrite=True)

    return outpath


def create_parser():
    parser = argparse.ArgumentParser(
        description='Subtract one FITS file from another, extension-wise '
                     '(file1 - file2), via FITStools.diff_hdulists().')
    parser.add_argument('file1', type=str, help='FITS file')
    parser.add_argument('file2', type=str, help='Subtracted FITS file')
    parser.add_argument('-tag', type=str, default='dsub',
                         help="Output filename tag; default='dsub'. "
                              "Output is written as TAG_<file1 basename> "
                              "in file1's directory.")
    parser.add_argument('-keys', type=str, nargs='+', default=None, metavar='KEY',
                         help='FITS header keyword(s) that must match '
                              'exactly, extension-wise, between the two files')
    parser.add_argument('-verbose', action='store_true', default=False,
                         help='Print the output filename after writing')
    return parser


def main():
    parser = create_parser()
    args = parser.parse_args()

    try:
        outpath = diff_files(args.file1, args.file2, tag=args.tag, keys=args.keys)
    except ValueError as e:
        sys.exit(f'subtract_fits: {e}')

    if args.verbose:
        print(outpath)


if __name__ == '__main__':
    main()
