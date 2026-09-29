#!/usr/bin/env python
# fits_table.py
# Author: Chaz Shapiro
#
# Print a human-readable table of FITS header values, one row per image
# extension, for one or more FITS files.  A thin CLI wrapper around
# pySQUID.FITStools.all_headers_to_df().
#
# Usage:
#   fits_table.py <file_or_pattern> [<file_or_pattern> ...] [options]
#
# Examples:
#   fits_table.py myfile.fits
#   fits_table.py 'data/run*.fits' -keys DETID GAINMODE
#   fits_table.py 'data/*.fits' -nexp -sort FILEBASE
#   fits_table.py 'data/*.fits' -list
#
# Options:
#   -keys KEY [KEY ...]  Extra FITS header keyword(s) to add to the table,
#                        on top of the hardcoded defaults below.
#   -nexp                If a file has N_EXPOS>1, show only its first
#                        extension instead of one row per exposure.
#   -sort KEY            FITS header keyword to sort the table by.
#                        Default: DATETIME, falling back to FILEBASE if
#                        DATETIME isn't present in the headers.
#   -list                Instead of the table, print every header keyword
#                        available across the matched file(s) and exit,
#                        at most 10 keys per line.
#
# Any requested keyword (default, via -keys, or via -sort) that is missing
# from a file's header is simply left blank -- this never raises an error.
#
# FILEBASE is FILENAME with the leading path and FITS extension stripped,
# e.g. "/data/run01.fits" -> "run01".  FITStools.extract_fits_keys() already
# writes this into every header it reads, so it's normally just passed
# through as-is; it's only re-derived here as a fallback (see
# add_filebase() below).

import argparse
import os
import sys

import pandas as pd

from pySQUID import FITStools as tools

# --------------------------------------------------------------------------
# Hardcoded FITS keywords always shown, in display order.  Add/remove keys
# here to change the script's default columns.
DEFAULT_KEYS = ['DATETIME', 'FILEBASE', 'N_EXPOS', 'EXTN','GAINMODE','TYPE','TIMDELAY','EXPTIME']

# Default keyword to sort rows by (overridable with -sort)
DEFAULT_SORT_KEY = 'DATETIME'

# If DEFAULT_SORT_KEY isn't available in the headers, fall back to sorting
# by this key instead (only applies to the default sort key, not to an
# explicit -sort KEY that happens to be missing).
DEFAULT_SORT_FALLBACK_KEY = 'FILEBASE'

# Max number of keywords per line when printing with -list
LIST_KEYS_PER_LINE = 10
# --------------------------------------------------------------------------


def add_filebase(df):
    '''
    Ensure a FILEBASE column exists: FILENAME with the leading path and
    FITS extension stripped (e.g. "/data/run01.fits" -> "run01").

    FITStools.extract_fits_keys() already computes exactly this for every
    header it reads, so the existing FILEBASE column (if present) is used
    as-is; FILENAME is only re-derived here as a fallback for headers that
    don't already have one.  Never raises -- if there's no FILENAME or
    FILEBASE to work from, df is returned unchanged and FILEBASE simply
    won't be available (it'll show up blank like any other missing key).
    '''
    if 'FILEBASE' in df.columns:
        return df

    if 'FILENAME' not in df.columns:
        return df

    df = df.copy()
    df['FILEBASE'] = df['FILENAME'].apply(
        lambda f: os.path.splitext(os.path.basename(str(f)))[0] if pd.notna(f) else f)

    return df


def collapse_to_first_extension(df):
    '''
    For files where N_EXPOS>1, keep only the row with the lowest EXTN (the
    first image extension) instead of one row per exposure.  Files without
    N_EXPOS, or with N_EXPOS<=1, are left untouched.

    Never raises if N_EXPOS/EXTN/FILENAME are missing from the headers --
    just returns df unchanged in that case.
    '''
    if not {'N_EXPOS', 'EXTN', 'FILENAME'}.issubset(df.columns):
        return df

    n_expos = pd.to_numeric(df['N_EXPOS'], errors='coerce')
    min_extn = df.groupby('FILENAME')['EXTN'].transform('min')
    keep = n_expos.isna() | (n_expos <= 1) | (df['EXTN'] == min_extn)

    return df[keep]


def sort_table(df, sort_key, fallback_key=None):
    '''
    Sort by sort_key if present and sortable; otherwise return df as-is
    (never raises).

    If sort_key isn't found and fallback_key is given (and is itself
    present in df), sort by fallback_key instead and print a note --
    e.g. build_table() uses this to fall back from DATETIME to FILEBASE.
    The fallback is only attempted once (no further fallback if
    fallback_key is also missing/unsortable).
    '''
    if not sort_key:
        return df

    sort_key = sort_key.upper()

    if sort_key not in df.columns:
        if fallback_key and fallback_key.upper() != sort_key and fallback_key.upper() in df.columns:
            print(f"Note: sort key '{sort_key}' not found in any header; "
                  f"sorting by '{fallback_key.upper()}' instead.", file=sys.stderr)
            return sort_table(df, fallback_key)
        print(f"Note: sort key '{sort_key}' not found in any header; "
              f"leaving table unsorted.", file=sys.stderr)
        return df

    try:
        return df.sort_values(by=sort_key, kind='stable', na_position='last')
    except TypeError:
        print(f"Note: could not sort on '{sort_key}' (mixed/unorderable "
              f"values); leaving table unsorted.", file=sys.stderr)
        return df


def build_table(filenames, extra_keys=None, nexp=False, sort_key=DEFAULT_SORT_KEY):
    '''
    Load headers from all image extensions of `filenames` (a single FITS
    filename/glob pattern, or a list of either) and return a DataFrame
    containing only DEFAULT_KEYS + extra_keys, ready for human-readable
    display.

    nexp=True collapses files with N_EXPOS>1 down to their first extension
    (see collapse_to_first_extension()).

    If sort_key is (or defaults to) DEFAULT_SORT_KEY and that key isn't
    available, sorting falls back to DEFAULT_SORT_FALLBACK_KEY.

    A requested keyword that doesn't exist in any header comes back as a
    blank column rather than raising an error.
    '''
    keys = list(DEFAULT_KEYS)
    for k in (extra_keys or []):
        k = k.upper()
        if k not in keys:
            keys.append(k)

    try:
        df = tools.all_headers_to_df(filenames)
    except ValueError:
        # all_headers_to_df had nothing to concatenate -- no files matched
        df = pd.DataFrame()

    if df.empty:
        return pd.DataFrame(columns=keys)

    df = add_filebase(df)

    if nexp:
        df = collapse_to_first_extension(df)

    fallback_key = DEFAULT_SORT_FALLBACK_KEY if sort_key and sort_key.upper() == DEFAULT_SORT_KEY else None
    df = sort_table(df, sort_key, fallback_key=fallback_key)

    # reindex() fills in any missing keyword as an all-blank column instead
    # of raising a KeyError -- this is what guarantees we never fail just
    # because a requested key doesn't exist in the headers.
    table = df.reindex(columns=keys)
    table = table.fillna('')

    return table


def list_available_keys(filenames):
    '''
    Return a sorted list of every header keyword available across the
    matched file(s), including FILEBASE.  Never raises; returns an empty
    list if nothing matches.
    '''
    try:
        df = tools.all_headers_to_df(filenames)
    except ValueError:
        # all_headers_to_df had nothing to concatenate -- no files matched
        return []

    if df.empty:
        return []

    df = add_filebase(df)

    # 'index' is pandas bookkeeping from all_headers_to_df's internal
    # reset_index(), not a real FITS keyword -- don't advertise it.
    return sorted(c for c in df.columns if c != 'index')


def print_key_list(keys, per_line=LIST_KEYS_PER_LINE, COLWIDTH=10):
    ''' Print keys one line at a time, at most per_line keys per line. '''
    for i in range(0, len(keys), per_line):
        rowlist = keys[i:i + per_line]
        for j, k in enumerate(rowlist):
            rowlist[j] = k + ' '*(COLWIDTH - len(rowlist[j])) # pad
        print('  '.join(rowlist))


def create_parser():
    parser = argparse.ArgumentParser(
        description='Print a human-readable table of FITS header values '
                     'for all image extensions in one or more FITS files.')
    parser.add_argument('filenames', type=str, nargs='+',
                         help='FITS filenames or patterns, '
                              'e.g. "data/*.fits"')
    parser.add_argument('-keys', type=str, nargs='+', default=[], metavar='KEY',
                         help='Additional FITS header keywords to include '
                              'in the table')
    parser.add_argument('-nexp', action='store_true', default=False,
                         help='Show only the first extension per file when '
                              'N_EXPOS>1 (default: show every extension)')
    parser.add_argument('-sort', type=str, default=DEFAULT_SORT_KEY, metavar='KEY',
                         help=f'FITS header keyword to sort rows by; '
                              f'default={DEFAULT_SORT_KEY} (falls back to '
                              f'{DEFAULT_SORT_FALLBACK_KEY})')
    parser.add_argument('-list', action='store_true', default=False,
                         help='Instead of the table, print every header '
                              'keyword available and exit')
    return parser


def main():
    parser = create_parser()
    args = parser.parse_args()

    if args.list:
        keys = list_available_keys(args.filenames)
        if not keys:
            print('No matching FITS files found.', file=sys.stderr)
            sys.exit(1)
        print_key_list(keys)
        return

    table = build_table(
        args.filenames,
        extra_keys=args.keys,
        nexp=args.nexp,
        sort_key=args.sort,
    )

    if table.empty:
        print('No matching FITS files found.', file=sys.stderr)
        sys.exit(1)

    print(table.to_string(index=False))


if __name__ == '__main__':
    main()
