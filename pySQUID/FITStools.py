# Helpful imports for RCtests

import astropy.io.fits as pf
import glob
import numpy as np
import os
import pandas as pd
import sys


GAIN_NOMINAL = 1.06   ### HIGH GAIN e/ADU before LSB drop
GAIN_LO_o_HI = 7.5 # Ratio of low to high conversion gains

SIGFIG = 4  # Max significant figures for decimals in results table

VOLTS_SINCE = (2, 0, 0)     # SCHEMA_V >= this: bias voltage keys are already in volts

def _schema_tuple(s):
    try:
        return tuple(int(x) for x in str(s).split('.'))
    except Exception:
        return (0, 0, 0)   # missing/unparseable -> treat as pre-schema

def check_dac_units(df):
    '''
    sys.exit(1) if SCHEMA_V indicates the bias voltage header keys (VLOW_ROW,
    VLOW_TG, VHIGH_TG, PIX_REF, VH_BIAS, V_EXTRA, V8OFFV) are still raw DAC
    counts, not volts. Call this immediately after loading headers, before
    SCHEMA_V/FILENAME get trimmed from the working DataFrame.
    '''
    if 'SCHEMA_V' not in df.columns:
        print("FATAL ERROR: No SCHEMA_V header found; cannot confirm bias voltages are in volts.")
        print("Re-run tdms_to_fits.py (schema >= 2.0.0) on the affected file(s) before analyzing.")
        sys.exit(1)

    legacy = df['SCHEMA_V'].apply(_schema_tuple).apply(lambda t: t < VOLTS_SINCE)
    if legacy.any():
        files = sorted(df.loc[legacy, 'FILENAME'].unique()) if 'FILENAME' in df.columns else []
        print(f"FATAL ERROR: {int(legacy.sum())} row(s) predate SCHEMA_V "
              f"{'.'.join(map(str, VOLTS_SINCE))}; bias voltages are raw DAC counts, "
              f"not volts, in: {files}.")
        print("Re-run tdms_to_fits.py (schema >= 2.0.0) on the affected file(s) before analyzing.")
        sys.exit(1)


def extract_fits_keys(filename):
    ''' Organize all extension headers into a Pandas DataFrame '''
    
    hdulist = pf.open(filename)

    # If not multi-extension, scrape the headers and quit
    if len(hdulist)==1:
        hdr = hdulist[0].header
        hdr['FILENAME'] = filename
        hdr['EXTN'] = 0
        hdr['FILEBASE'] = os.path.splitext(os.path.basename(filename))[0]
        return pd.DataFrame([hdr])

    hdrlist = [hdu.header for hdu in hdulist]

    for i, hdr in enumerate(hdrlist):
        if i==0: continue
        # Append 0th extension headers to all others
        for k,v in hdrlist[0].items(): 
            if k not in hdr: hdr[k]=v
                
        # Include filename and extension number
        hdr['FILENAME'] = filename
        hdr['EXTN'] = i
        hdr['FILEBASE'] = os.path.splitext(os.path.basename(filename))[0]
            
    return pd.DataFrame(hdrlist[1:])

def all_headers_to_df(list_of_filenames):
    ''' Loop over filenames or patterns and concat all headers into Pandas DataFrame '''
    
    # Convert argument to list is not already a list
    flist = list_of_filenames if pd.api.types.is_list_like(list_of_filenames) else [list_of_filenames]
        
    flist = [glob.glob(pattern) for pattern in flist]
    flist = sum(flist, [])
    
    flist = sorted(flist)
    
    headers_per_file = [extract_fits_keys(f) for f in flist]
    headers_per_file = pd.concat(headers_per_file).reset_index()

    # Remove blank headers if they creep in
    if '' in headers_per_file.columns:
        headers_per_file = headers_per_file.drop(columns=[''])

    return headers_per_file

def kgain(PIXELT, SETTLET, LSB_DROP):
    return GAIN_NOMINAL * 2**LSB_DROP / (PIXELT-SETTLET)

def ADU_to_V(PIXELT, SETTLET, LSB_DROP):
    # return kgain(PIXELT, SETTLET) / GAIN_NOMINAL / (1.8 * 2**13)
    return 1./ (PIXELT-SETTLET) / (1.8 * 2**13) * 2**LSB_DROP # LSBs dropped in BIN to FITS conversion


def passfail(mybool):
    pf = {True: 'PASS', False:'FAIL'}
    try:
        return np.array([pf[b] for b in mybool])
    except TypeError:  # Not listlike?
        return pf[mybool]

def fast_median(a: np.ndarray) -> float:
    """
    O(n) median via np.partition (introselect) instead of np.median's O(n log n)
    full sort.  Significantly faster on large arrays, especially after clipping
    has already reduced the array size.
    """
    n = len(a)
    if n % 2:
        return float(np.partition(a, n // 2)[n // 2])
    h = n // 2
    p = np.partition(a, [h - 1, h])
    return 0.5 * (float(p[h - 1]) + float(p[h]))


def sigma_clip(flat: np.ndarray, sigma: float) -> np.ndarray:
    """
    Iterative sigma-clip on a pre-flattened array.
    Shrinks the array each iteration (fastest strategy for typical FITS sizes).
    Converges when no further pixels are rejected.
    """
    while True:
        mean = flat.mean()
        std  = flat.std()
        if std == 0:
            break
        mask = np.abs(flat - mean) <= sigma * std
        if mask.all():
            break
        flat = flat[mask]
    return flat

def custom_sig_figs(val):
    # Pass non-numeric or missing data through untouched
    if not isinstance(val, (int, float)) or pd.isna(val) or np.isinf(val) or val == 0:
        return val

    # Find the order of magnitude
    magnitude = int(np.floor(np.log10(abs(val))))

    # If N sig figs are before the decimal (magnitude >= N-1), round to nearest int
    if magnitude >= SIGFIG-1:
        return f"{round(val):.0f}"

    # Otherwise, display exactly N significant figures
    else:
        decimals = SIGFIG-1 - magnitude
        return f"{val:.{decimals}f}"


def compute_stats(arr: np.ndarray, clip_sigma=0, do_median=False, quantiles=[]) -> dict:
    """
    Return mean, (optionally median,) stddev, and mean/stddev for an array.
    Flattens to float32; optionally sigma-clips.
    quantiles is a list if percentages (0--100), e.g. [90,99, 99.9]
    """
    flat = arr.ravel().astype(np.float32)  # convert ADUf to ADUe if given
    if clip_sigma: # skips if None or 0
        flat = sigma_clip(flat, clip_sigma)
    mean = flat.mean()
    std  = flat.std()
    row = {"count": arr.size}
    row["mean"] = mean
    if do_median:
        row["med"] = fast_median(flat)
    row["std"]                             = std
    row["mean/std"]                        = mean / std

    for k in quantiles:
        row[str(k)] = np.quantile(flat, k/100.)
        
    return row

def compute_baseline_stats(row, roi=None, verbose=False, adu_convert=1, **kwargs):
    if verbose: print(row['FILENAME'], row['EXTNAME'])
    arr = pf.getdata(row['FILENAME'], row['EXTN'])[0] *adu_convert # [0] selects baseline image
    return compute_stats(arr[roi], **kwargs)

def get_cds_image(row, roi=None, verbose=False, gainfits=False):
    ''' Detect whether image is 2D or 3D.  If 3D, apply CDS subtraction ''' 

    if verbose: print(row['FILENAME'], row['EXTNAME'])
    kgain = row['GAINFITS'] if gainfits else int(1)

    arr = pf.getdata(row['FILENAME'], row['EXTN']).astype(np.int16) # enable values <0

    if arr.ndim==3 and len(roi)==3:
        roi3D = roi
    elif arr.ndim==3 and len(roi)==2:
        roi3D = (slice(None),) + roi
    elif arr.ndim==2 and len(roi)==3:
        roi3D = roi[1:]
    elif arr.ndim==2 and len(roi)==2:
        roi3D = roi
    else:
        print('UNKNOWN IMAGE FORMAT')
        import sys
        sys.exit(-1)

    # roi3D = roi if len(roi)==3 else (slice(None),) + roi
    if len(roi3D) != arr.ndim:
        sys.exit('Mismatched image and ROI dimensions')

    arr = arr[roi3D]  # Extract region before CDS subtraction (for efficiency)
    img = arr[1] - arr[0] if arr.ndim == 3 else arr
    img = img * kgain
    return img

def compute_cds_stats(row, roi=None, verbose=False, gainfits=False, **kwargs):

    img = get_cds_image(row, roi=roi, verbose=verbose, gainfits=gainfits)
    return compute_stats(img, **kwargs)

def compute_cds_histogram(row, roi=None, verbose=False, gainfits=False, **kwargs):

    img = get_cds_image(row, roi=roi, verbose=verbose, gainfits=gainfits)
    hist = np.histogram(img, **kwargs)
    return hist[0]
