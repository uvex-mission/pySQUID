#!/usr/bin/python3
# tdms_to_fits.py
#
# Converts split TDMS files to a FITS image sequence, optionally saving
# an intermediate .bin file.  If a .bin file from a prior run is passed
# as the input argument, TDMS reading is skipped and the binary data is
# converted to FITS directly.
#
# Usage:
#   tdms_to_fits.py <input_basename> [options]
#
# Arguments:
#   input_basename   Base path of the TDMS file (with or without .tdms extension),
#                    or path to a previously-saved .bin file.
#                    Split TDMS files (<base>_0001.tdms, etc.) are included automatically.
#
# Options:
#   --outdir DIR     Output directory for .fits (and optionally .bin) files.
#                    Default: same directory as input.
#   --metadata FILE  Path to .yaml metadata file.
#                    Default: <input_basename>.yaml if it exists.
#   --bin            Also save the intermediate .bin file (TDMS input only).
#   --bin-only       Save .bin file only; do not convert to FITS (TDMS input only).
#   --exposures N    Process only the first N exposures.
#   --fast           Reuse block structure from exposure 0 for all subsequent exposures.
#   --stream         Low-memory mode: write each exposure to FITS as it is built.
#                    Implies --fast.
#
# Examples:
#   tdms_to_fits.py /data/myrecording --outdir /data/output --fast
#   tdms_to_fits.py /data/myrecording --bin-only --outdir /data/bin_output
#   tdms_to_fits.py /data/myrecording.bin --fast

### TODOS:
# DATETIME header from server
# LED on/off states

from collections import namedtuple
import sys
import os
import glob
import argparse
import tempfile
import numpy as np
from astropy.io import fits
import yaml
# from datetime import datetime
from nptdms.reader import TdmsReader


# Parameter names from the 66-byte frame header
FRAME_HEADER_PARAM_NAMES = [
    "STROW", "ENDROW", "ADCCLKT", "SETTLET", "PIXELT", "ROWT",
    "FRAMETL", "FRAMETM", "XENBLKST", "XENBLKED", "XLATCHST", "XLATCHED",
    "XADDRINC", "FRMCNT", "TESTEN", "TESTSEL", "TESTPAT", "CALDAC",
    "M_BLOCKS", "NOGLO", "VLOW_ROW", "VLOW_TG", "VHIGH_TG", "PIX_REF",
    "VH_BIAS", "V_EXTRA", "V8OFFV", "MSEC", "PICO_XORD", "X_OFFSET"
]

HEADER_SIZE_BYTES = 66
BLOCK_SIZE_WORDS  = 27
BLOCK_SIZE_BYTES  = 54
WIDTH             = 4096  # Image width (columns) in pixels
HEIGHT            = 4096  # Image height (rows) in pixels

GAINELX_NOM       = {'HIGH':1.1, 'LOW':1.1*7.1}   # Nominal gains e-/ADUe (electronics ADU) for a single BBX pixel sample
LSB_DROP          = 3     # Number of least-significant bits to drop in binary to FITS conversion
KSCALE            = 4     # Divide data by even integer to reduce file size; affects FITS gain (e-/ADU)

PIPELINE_SCHEMA_VERSION = "2.0.0"  # bumped: DAC_VOLTAGE_KEYS now stored in volts, not raw DN

VARIABLE_PARAMS = ["FRMCNT", "MSEC"]
CONSTANT_PARAMS = [p for p in FRAME_HEADER_PARAM_NAMES if p not in VARIABLE_PARAMS]

# Sidecar YAML supplying FITS keyword comments
_COMMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "fits_keyword_comments.yaml")
try:
    with open(_COMMENTS_PATH) as _f:
        KEYWORD_COMMENTS      = yaml.safe_load(_f)
except FileNotFoundError:
    KEYWORD_COMMENTS = {}
    print(f"WARNING: {_COMMENTS_PATH} not found; FITS keywords will have no comments.")

def apply_keyword_comments(header):
    """Write comments from KEYWORD_COMMENTS into every matching key in header."""
    for key, comment in KEYWORD_COMMENTS.items():
        if key in header:
            header.comments[key] = comment

def DAC_to_V(dac, key=None):
    '''
    Convert a raw DAC/ADC telemetry count to an approximate bias voltage.

    The board reports an exact dac value -- it is preserved in the header comment (see
    build_ext_header()) precisely because this conversion is not exact.
    The slope below (3.76V / 768 DN) is a nominal calibration figure
    shared across several bias channels.

    Two channels apply additional scalings on top of the
    base conversion -- pass the FITS keyword name to select the
    right case:
      - VLOW_TG: V = -0.243*V_base + 0.6   (inverted, level-shifted)
      - V8OFFV:  V = max(2*V_base, 4.9)    (doubled, hits max)
    All other keys (VLOW_ROW, VHIGH_TG, PIX_REF, VH_BIAS, V_EXTRA) use the
    base conversion unmodified.
    '''
    slope = 3.76 / 768
    v = dac * slope
    if key == "VLOW_TG":
        v = -0.243 * v + 0.6
    elif key == "V8OFFV":
        v = max(2 * v, 4.9)
    return v

# Frame-telemetry keys reported as bias DAC counts; stored in volts as of schema 2.0.0
DAC_VOLTAGE_KEYS = {"VLOW_ROW", "VLOW_TG", "VHIGH_TG", "PIX_REF", "VH_BIAS", "V_EXTRA", "V8OFFV"}

# ---------------------------------------------------------------------------
# TDMS reading
# ---------------------------------------------------------------------------

def collect_tdms_files(base_path):
    """Return [base.tdms, base_0001.tdms, ...] in order."""
    base_path = base_path.rstrip("/")
    if base_path.lower().endswith(".tdms"):
        base_path = base_path[:-5]
    primary = base_path + ".tdms"
    if not os.path.exists(primary):
        raise FileNotFoundError(f"Base TDMS file not found: {primary}")
    split_files = sorted(glob.glob(base_path + "_[0-9][0-9][0-9][0-9].tdms"))
    return base_path, [primary] + split_files


def read_tdms_segment_data(tdms_path, out_file):
    """
    Read NI-DAQmx digital data directly from raw segment bytes and write to out_file.

    The 16 digital lines are stored as packed uint16 words (bit N = channel at
    raw_bit_offset N).  The raw words have port0 in the low byte (bits 0-7) and
    port1 in the high byte (bits 8-15).  A byteswap is applied to match the
    layout expected by the downstream frame parser (port1 low, port0 high),
    consistent with the original merge_tdms_to_bin.py packing.
    """
    with open(tdms_path, 'rb') as f:
        reader = TdmsReader(f)
        reader.read_metadata()
        for seg in reader._segments:
            data_size = seg.next_segment_pos - seg.data_position
            if data_size <= 0:
                continue
            f.seek(seg.data_position)
            raw = np.frombuffer(f.read(data_size), dtype=np.uint16)
            out_file.write(raw.byteswap().tobytes())


def tdms_to_raw(all_tdms_files, bin_path=None):
    """
    Convert TDMS files to raw binary data.

    If bin_path is given, write to that file and return a memmap of it.
    Otherwise write to a temporary file, load into memory, and delete it.
    Returns a numpy uint8 array of the raw binary data.
    """
    if bin_path is not None:
        with open(bin_path, "wb") as out_file:
            for tdms_path in all_tdms_files:
                print(f"    Processing: {tdms_path}")
                read_tdms_segment_data(tdms_path, out_file)
        return np.memmap(bin_path, dtype=np.uint8, mode='r')
    else:
        # Write to a temp file, load, delete — avoids holding 2GB in RAM
        # while also not leaving a permanent .bin on disk.
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp_path = tmp.name
            for tdms_path in all_tdms_files:
                print(f"    Processing: {tdms_path}")
                read_tdms_segment_data(tdms_path, tmp)
        try:
            return np.memmap(tmp_path, dtype=np.uint8, mode='r')
        finally:
            # Register cleanup — memmap keeps file open so we unlink now;
            # the OS will reclaim space once the memmap is released.
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# bin_to_fits helpers (unchanged from bin_to_fits.py)
# ---------------------------------------------------------------------------

def get_pico_desc(pico_val):
    if pico_val is None: return "UNKNOWN"
    return " | ".join([
        "RST_CLK Low" if (pico_val & 1) else "RST_CLK High",
        "400us Delay" if (pico_val & 2) else "No Delay",
        "No TG"       if (pico_val & 4) else "TG Present",
    ])

def gainmode_from_pico(pico_val):
    if pico_val is None: return 'UNKNOWN'
    if pico_val < 0:     return 'UNKNOWN'
    if pico_val < 8:     return 'HIGH'
    else:                return 'LOW'

def get_xorder_desc(xord_val):
    if xord_val is None: return "UNKNOWN"
    if xord_val == 0:    return "Normal Sequential"
    if xord_val == 1:    return "Gray Code"
    if xord_val == 2:    return "Mod 256"
    return "Scrambled"

def unpack_24bit_array(payload_words, lsb_drop=LSB_DROP):
    """
    Unpack pixel words into final 16-bit pixel values (24-bit raw >> lsb_drop).
    Input:  (N, 24) uint16 — N blocks, 24 words each (must be C-contiguous)
    Output: (N, 16) uint16 — N blocks, 16 pixels each, already shifted
    """
    N        = payload_words.shape[0]
    out      = np.empty((N, 16), dtype=np.uint16)
    triplets = payload_words.reshape(N, 8, 3).astype(np.int32)
    out[:, 0::2] = ((triplets[:, :, 0] << 8) | (triplets[:, :, 1] >> 8)) >> lsb_drop
    out[:, 1::2] = (((triplets[:, :, 1] & 0x00FF) << 16) | triplets[:, :, 2]) >> lsb_drop
    return out

def find_frame_offsets(data, limit=None):
    """Find all frame sync markers (FF FF A5 A5) in raw data."""
    sample = data[:limit] if limit else data
    c_idx  = np.where(sample[:-3] == 0xFF)[0]
    c_idx  = c_idx[(sample[c_idx+1] == 0xFF) &
                   (sample[c_idx+2] == 0xA5) &
                   (sample[c_idx+3] == 0xA5)]
    return c_idx

# def parse_filename_date(filename):
#     """Parse YYYYMMDD_HHMMSS from filename, falling back to now."""
#     try:
#         parts = os.path.basename(filename).split('_')
#         return datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S").isoformat()
#     except Exception:
#         return datetime.now().isoformat()

def assign_scan_indices(rows, cols, num_scans):
    """Return the scan index (0..num_scans-1) for every block."""
    keys         = rows.astype(np.int64) * 256 + cols.astype(np.int64)
    sort_order   = np.argsort(keys, kind='stable')
    sorted_keys  = keys[sort_order]
    _, inverse, counts = np.unique(sorted_keys, return_inverse=True, return_counts=True)
    run_starts         = np.zeros(len(counts), dtype=np.int64)
    run_starts[1:]     = np.cumsum(counts[:-1])
    sorted_scan_idx = (np.arange(len(sorted_keys), dtype=np.int64)
                       - run_starts[inverse]).astype(np.int16)
    if np.any(sorted_scan_idx >= num_scans):
        bad      = np.argmax(sorted_scan_idx >= num_scans)
        bad_orig = sort_order[bad]
        print(f"\nFATAL ERROR: Overscan detected on Row {rows[bad_orig]}, Col {cols[bad_orig]}.")
        print(f"Data contains {int(sorted_scan_idx[bad])+1} hits for this pixel, "
              f"but YAML configured num_scans={num_scans}.")
        print("Please update 'num_scans' in your YAML to match the experiment format.")
        sys.exit(1)
    return sorted_scan_idx[np.argsort(sort_order)]


ScatterPlan = namedtuple('ScatterPlan', ['actual_rows', 'per_slice'])


def build_exposure_cube(blocks, num_scans, strow, endrow,
                        scatter_plan=None, lsb_drop=LSB_DROP):
    """Decode one exposure's blocks into a (num_scans, actual_rows, WIDTH) uint16 cube."""

    # Compute number of detector rows based on the
    # start/end row values parsed from the frame header.
    actual_rows = endrow - strow + 1
    if (endrow!=strow) and (actual_rows <= 0):
        actual_rows += HEIGHT

    # Ensure the block array is C-contiguous so that downstream numpy views and
    # reshapes work correctly without implicit copies.
    blocks      = np.ascontiguousarray(blocks)

    # Each 27-word block has a small header.  Words h2 and h3 encode the detector
    # row and column-group (col-block) address of the block's pixels.
    h2          = blocks[:, 1]
    h3          = blocks[:, 2]

    # Reconstruct the row address from bits spread across h2 (low nibble) and h3
    # (high byte), then subtract strow to convert from absolute detector row to a
    # zero-based index within this exposure's row range.
    rows        = (((h2 & 0x000F) << 8) | (h3 >> 8)) - strow

    # The low byte of h3 gives the column-block index (0-15 in a 4096-wide sensor
    # divided into 256-pixel groups); cols selects which 256-pixel slice applies.
    cols        = h3 & 0x00FF

    # Words 3-26 of each block (24 words) carry 16 packed 24-bit pixel samples.
    # Reshape to (N_blocks, 24) and unpack to (N_blocks, 16) uint16 values.
    pixels_16   = unpack_24bit_array(blocks[:, 3:27].reshape(-1, 24), lsb_drop)

    # Discard any blocks whose decoded row falls outside [0, actual_rows).
    # This can happen near exposure boundaries or due to data corruption.
    valid       = (rows >= 0) & (rows < actual_rows)
    if not np.all(valid):
        rows, cols, pixels_16 = rows[valid], cols[valid], pixels_16[valid]

    # Allocate the 3-D output cube: axis 0 = scan index, axis 1 = row,
    # axis 2 = column.  Initialised to zero so missing pixels stay at 0.
    exposure_cube = np.zeros((num_scans, actual_rows, WIDTH), dtype=np.uint16)

    # --- Scatter plan: build or reuse ---
    # The scatter plan precomputes, for every (pixel-slice, scan) combination,
    # the sorted arrays of (source block index, destination row, destination x)
    # needed to scatter pixels into the cube.  Building the plan is expensive
    # (it calls assign_scan_indices and sorts), so in --fast mode the caller
    # uses the plan built for exposure 0 and skips to the scatter step below.
    if scatter_plan is None:

        # Determine which scan repetition (0..num_scans-1) each block belongs to.
        # Blocks are grouped by (row, col) key; within each group, blocks are
        # labelled 0, 1, ... in the order they appear.
        scan_indices = assign_scan_indices(rows, cols, num_scans)
        per_slice = []
        for i in range(16):
            # Each of the 16 pixel positions within a block maps to a unique
            # x-coordinate: pixel i of a col-block at index `cols` lands at
            # column cols*256 + i in the full-width image.
            target_xs = cols + i * 256
            x_valid   = target_xs < WIDTH  # guard against out-of-range columns
            s_entries = []
            for s_idx in range(num_scans):
                # Select only the blocks that belong to this pixel-slice and scan.
                mask    = x_valid & (scan_indices == s_idx)
                src     = np.where(mask)[0]    # indices into the blocks/pixels arrays
                row_idx = rows[mask]
                x_idx   = target_xs[mask]

                # Sort by (row, x) so the scatter writes are as cache-friendly as
                # possible when filling the output cube.
                order   = np.lexsort((x_idx, row_idx))
                s_entries.append((src[order], row_idx[order], x_idx[order]))
            per_slice.append(s_entries)

        # Store the plan for potential reuse by subsequent exposures (--fast mode).
        scatter_plan = ScatterPlan(actual_rows=actual_rows, per_slice=per_slice)

    # --- Scatter pixels into the cube ---
    # Use the precomputed index arrays to place each pixel directly into its
    # (scan, row, col) position in the output cube.  `i` is the within-block
    # pixel position (selects column pixels_16[:, i]); s_idx is the scan layer.        
    for i, s_entries in enumerate(scatter_plan.per_slice):
        for s_idx, (src, row_idx, x_idx) in enumerate(s_entries):
            if src.size:
                exposure_cube[s_idx, row_idx, x_idx] = pixels_16[src, i]

    return exposure_cube, scatter_plan


def build_ext_header(hdu, exp_idx, is_complete, num_scans,
                     TIMMISC_last, header_params_raw):
    """Populate the per-exposure ImageHDU header with telemetry"""
    h = hdu.header
    h['EXP_ID']  = exp_idx
    h['COMPLETE'] = is_complete

    # MISC parameters
    for p_idx, name in enumerate(FRAME_HEADER_PARAM_NAMES):
        if p_idx >= len(header_params_raw):
            break
        val = int(header_params_raw[p_idx])
        if name == "PICO_XORD":
            pico_ver = (val >> 8) & 0xFF
            x_order  = val & 0xFF
            h['PICO_VER'] = pico_ver
            h['PICO_DES'] = get_pico_desc(pico_ver)
            h['X_ORDER']  = x_order
            h['X_ORDERD'] = get_xorder_desc(x_order)
        elif name in DAC_VOLTAGE_KEYS:
            h[name[:8]] = (round(DAC_to_V(val, key=name), 4), f'[V] Board Param: {name} (raw DN={val})')
        else:
            h[name[:8]] = (val, f'Board Param: {name}')

    # Timing parameters
    h['TIMMISC_'] = h['FRMCNT'] + h['MSEC']/1000.
    h['TIMEXP_'] = h['TIMMISC_']-TIMMISC_last if TIMMISC_last>0 else -1.
    h['TIMEXP_'] = round(h['TIMEXP_'], 3)
    h['EXPTIME'] = h['TIMEXP_']

    # GAIN parameters;  A * "A2B" = B
    h['GAIN_F2B'] = int(KSCALE*2**LSB_DROP)         # ADUb/ADUf = GAINFITS/GAINBIN
    h['GAIN_E2B'] = int(h['PIXELT']-h['SETTLET'])   # ADUb/ADUe  ; convert ADUe to BIN (sum N samples)
    h['GAIN_F2E'] = h['GAIN_F2B'] / h['GAIN_E2B']   # ADUe/ADUf = F2E = F2B*B2E = F2B/E2B = GAINFITS/GAINELX_NOM

    h['GAINMODE'] = gainmode_from_pico(pico_ver)    # HIGH or LOW
    h['GAINELX'] = GAINELX_NOM[h['GAINMODE']]       # Nominal e-/ADUe (electronics ADU) for single BBX pixel sample
    h['GAINBIN'] = h['GAINELX'] / h['GAIN_E2B']     # e-/ADUb for BIN file = B2e- = B2E*E2e- = E2e-/E2B
    h['GAINFITS'] = h['GAINBIN']*h['GAIN_F2B']      # e-/ADUf for FITS file = F2e- = F2B*B2e- = F2B*GAINBIN

    for k in ['GAINBIN','GAINFITS','GAIN_F2E']: h[k] = round(h[k], 4)
    apply_keyword_comments(h)


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert_to_fits(raw_data, source_name, yaml_path, output_dir,
                    max_exposures=None, fast=False, stream=False, skip_exposures=1):
    """Convert a raw uint8 binary array to a FITS sequence."""
    os.makedirs(output_dir, exist_ok=True)

    metadata = {}
    # if yaml_path and os.path.exists(yaml_path):
    try:
        with open(yaml_path, 'r') as f:
            metadata = yaml.safe_load(f) or {}
            fits_headers = metadata['fits_headers']
    except Exception as e:
        print(f"WARNING: Failed to load FITS headers from YAML {yaml_path}: {e}")
        fits_headers = {}

    num_scans      = int(metadata.get('num_scans', 2))

    try:
        frame_offsets = find_frame_offsets(raw_data)
        total_found       = len(frame_offsets)

        if skip_exposures > 0:
            print(f"Skipping first {skip_exposures} exposures.")
            if skip_exposures >= total_found:
                print("Error: Skip count exceeds frames found.")
                return
            frame_offsets = frame_offsets[skip_exposures:]

        selected_indices = metadata.get('select_exposures')
        if selected_indices is not None:
            if isinstance(selected_indices, int):
                selected_indices = [selected_indices]

            valid_offsets = []
            for idx in selected_indices:
                if 0 <= idx < len(frame_offsets):
                    valid_offsets.append(frame_offsets[idx])
                else:
                    print(f"  Warning: Selected index {idx} out of range "
                          f"(max {len(frame_offsets)-1}).")
            frame_offsets = valid_offsets
            print(f"Plucking {len(frame_offsets)} specific exposures: {selected_indices}")

        elif max_exposures:
            frame_offsets = frame_offsets[:max(1, int(max_exposures))]

        # Summarize the job
        num_exposures = len(frame_offsets)
        mode_flags = []
        if fast:   mode_flags.append("fast: reusing block structure from exposure 0")
        if stream: mode_flags.append("stream: low-memory append mode")
        flag_str = f" [{', '.join(mode_flags)}]" if mode_flags else ""
        print(f"Processing {num_exposures} exposures ({num_scans} scans/exp){flag_str}.")

        # Construct PRIMARY FITS headers from metadata
        primary_hdu = fits.PrimaryHDU()
        head        = primary_hdu.header
        head['SCHEMA_V'] = PIPELINE_SCHEMA_VERSION
        # head['DATE-OBS'] = parse_filename_date(source_name)  ### SHOULD COME FROM SERVER TIMESTAMP
        head['FILENAME'] = os.path.basename(source_name)
        head['N_EXPOS']  = num_exposures
        head['SKIPPED']  = skip_exposures
        head['LSB_DROP'] = LSB_DROP
        head['KSCALE']   = KSCALE

        # User-provided headers
        for k, v in fits_headers.items(): head[k]=v
        apply_keyword_comments(head)

        base_name = os.path.splitext(os.path.basename(source_name))[0]
        out_path  = os.path.join(output_dir, base_name + ".fits")

        if stream:
            primary_hdu.writeto(out_path, overwrite=True)

        hdul_list    = None if stream else [primary_hdu]
        scatter_plan = None

        TIMMISC_last = -1  # MISC clock time to compare with next extension's TIMMISC

        sentinels = np.append(frame_offsets[1:], raw_data.size)

        for exp_idx, (offset, next_offset) in enumerate(zip(frame_offsets, sentinels)):

            offset, next_offset = int(offset), int(next_offset)

            if next_offset - offset < HEADER_SIZE_BYTES:
                print(f"  Warning: Exposure {exp_idx} is too small. Skipping.")
                continue

            header_params_raw = raw_data[offset + 6 : offset + HEADER_SIZE_BYTES].view('>u2')
            strow  = int(header_params_raw[0])
            endrow = int(header_params_raw[1])
            print(f"Processing Exposure {exp_idx+1}/{num_exposures} "
                  f"(FRMCNT: {header_params_raw[13]}, Rows: {strow}-{endrow})...")

            expected_rows   = endrow - strow + 1 if endrow >= strow else 0
            expected_blocks = expected_rows * 256 * num_scans
            data_bytes      = raw_data[offset + HEADER_SIZE_BYTES : next_offset]
            num_blocks      = data_bytes.size // BLOCK_SIZE_BYTES
            is_complete     = num_blocks >= expected_blocks

            if not is_complete and expected_blocks > 0:
                print(f"  Warning: Data underfill. Found {num_blocks} blocks, "
                      f"expected {expected_blocks} for {expected_rows} rows "
                      f"and {num_scans} scans.")
            elif expected_blocks > 0 and num_blocks > expected_blocks:
                num_blocks = expected_blocks

            if num_blocks == 0:
                print(f"  Skipping Exposure {exp_idx+1}: No data blocks.")
                continue

            # Re-interpret the raw bytes as big-endian uint16 words, then reshape
            # into a (num_blocks, BLOCK_SIZE_WORDS) matrix so that each row is one
            # complete 27-word block ready for build_exposure_cube() to process.
            blocks = (data_bytes[:num_blocks * BLOCK_SIZE_BYTES]
                      .view('>u2')
                      .reshape(num_blocks, BLOCK_SIZE_WORDS))

            # Build the 3-D pixel cube for this exposure.
            # scatter_plan controls whether the expensive per-block index
            # computation is performed:
            #   - None  -> always compute fresh (default / non-fast mode).
            #   - reuse -> pass the plan from exposure 0 (--fast mode).
            # The returned scatter_plan is stored so it can be forwarded to the
            # next iteration when fast=True.
            exposure_cube, scatter_plan = build_exposure_cube(
                blocks, num_scans, strow, endrow,
                scatter_plan if fast else None, LSB_DROP)

            # Convert ADU to e- (roughly) to reduce data type; GAINFITS header gives approximate e-/ADU
            # Python division '/' returns float; '//' returns int
            exposure_cube += KSCALE//2 # add an offset so the result is rounded to nearest int, not floored
            exposure_cube //= KSCALE  # Integer division implies floor()

            # Wrap the cube in a FITS ImageHDU.  The cube's three axes are
            # (num_scans, actual_rows, WIDTH); FITS stores them in Fortran order,
            # so the extension will appear as (WIDTH, actual_rows, num_scans) in NAXIS keywords 
            hdu = fits.ImageHDU(exposure_cube, name=f"EXP_{exp_idx:03d}")

            # Use MISC clock to estimate exposure time (time since last scan)
            # Primary hdu will have no info, so 1st scan will default to -1
            build_ext_header(hdu, exp_idx, is_complete, num_scans, TIMMISC_last, header_params_raw)
            TIMMISC_last = hdu.header['TIMMISC_']  # update for next iteration

            if stream:
                # --stream mode: append the HDUs to the FITS file as we go
                # Slower but reduces RAM usage to ~1 exposure at a time
                with fits.open(out_path, mode='append', memmap=False) as f:
                    f.append(hdu)
                del exposure_cube, hdu
            else:
                # Accumulate all HDUs in memory; single writeto() call
                hdul_list.append(hdu)

        if stream:
            print(f"Done. FITS file: {out_path}")
        else:
            print(f"Writing FITS file...")
            fits.HDUList(hdul_list).writeto(out_path, overwrite=True)
            print(f"Done. FITS file: {out_path}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert split TDMS files (or a previously-saved .bin file) "
            "to a FITS image sequence."
        )
    )
    parser.add_argument(
        "input_basename",
        help=(
            "Base path of the TDMS file (with or without .tdms extension), "
            "or path to a .bin file produced by a prior --bin / --bin-only run."
        )
    )
    parser.add_argument(
        "--outdir", "-o", default=None,
        help="Output directory (default: same directory as input)."
    )
    parser.add_argument(
        "--metadata", "-m", default=None,
        help="Path to .yaml metadata file (default: <input_basename>.yaml if present)."
    )
    parser.add_argument(
        "--bin", action="store_true",
        help="Also save the intermediate .bin file (TDMS input only)."
    )
    parser.add_argument(
        "--bin-only", action="store_true",
        help="Save .bin file only; do not convert to FITS (TDMS input only)."
    )
    parser.add_argument(
        "--exposures", "-n", dest="max_exposures", type=int, default=None,
        help="Process only the first N extensions"
    )
    parser.add_argument(
        "--skip", type=int, default=0,
        help="Skip the first N images extensions"
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Reuse block structure from exposure 0 for all subsequent exposures."
    )
    parser.add_argument(
        "--stream", action="store_true",
        help="Low-memory mode: write each exposure to FITS as it is built."
    )
    args = parser.parse_args()

    fast   = args.fast #or args.stream
    stream = args.stream

    input_arg = args.input_basename.rstrip("/")

    # ------------------------------------------------------------------
    # Produce raw_data and base_name.  The two branches differ only in
    # how the binary data is obtained; everything after this block is
    # shared regardless of whether we started from TDMS or .bin.
    # ------------------------------------------------------------------
    if input_arg.lower().endswith(".bin"):
        # --- .bin input: skip TDMS conversion entirely ---
        if not os.path.exists(input_arg):
            print(f"Error: .bin file not found: {input_arg}")
            sys.exit(1)
        if args.bin or args.bin_only:
            print("Warning: --bin and --bin-only are ignored when the input is already a .bin file.")

        base_name = os.path.splitext(os.path.basename(input_arg))[0]
        input_dir = os.path.dirname(os.path.abspath(input_arg))
        print(f"### Reading .bin file: {input_arg}")
        raw_data  = np.memmap(input_arg, dtype=np.uint8, mode='r')

    else:
        # --- TDMS input: collect files, optionally write .bin ---
        base_path, all_files = collect_tdms_files(input_arg)
        base_name = os.path.basename(base_path)
        input_dir = os.path.dirname(os.path.abspath(base_path))

        print(f"### Found {len(all_files)} TDMS file(s):")
        for f in all_files:
            print(f"    {f}")

        # out_dir is needed for bin_path, so resolve it here before the
        # early return so --bin-only can write to the right place.
        out_dir  = args.outdir if args.outdir else input_dir
        bin_path = os.path.join(out_dir, base_name + ".bin") if (args.bin or args.bin_only) else None

        print(f"### Reading TDMS data...")
        raw_data = tdms_to_raw(all_files, bin_path=bin_path)
        if args.bin_only:
            print(f"### Done. Binary file: {bin_path}")
            return

        if bin_path:
            print(f"### Binary file saved: {bin_path}")

    # Resolve output directory, YAML, then convert to FITS.
    out_dir = args.outdir if args.outdir else input_dir
    os.makedirs(out_dir, exist_ok=True)

    yaml_path = args.metadata
    if yaml_path is None:
        candidate = os.path.join(input_dir, base_name + ".yaml")
        if os.path.exists(candidate):
            yaml_path = candidate

    print(f"### Converting to FITS...")
    convert_to_fits(raw_data, base_name, yaml_path, out_dir,
                    args.max_exposures, fast, stream, args.skip)


if __name__ == "__main__":
    main()
