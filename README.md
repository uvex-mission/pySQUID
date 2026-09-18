# pySQUID

Control and data-reduction tools for the UVEX SQUID camera testbed.

pySQUID is a small toolkit for operating a UVEX detector testbed camera and for
turning its raw output (TDMS acquisitions, raw CMOS FITS frames) into
science-ready FITS files. It installs as a normal Python package and adds
three commands to your environment:

- `pysquid` — drops you into an interactive IPython shell with a `Camera`
  object already connected to your testbed, using settings from a YAML
  config file you provide.
- `cds` — digitally CDS-subtracts a raw CMOS `M x N K` image (BBx camera),
  with optional frame stacking and dual-gain splitting.
- `tdms` — converts split TDMS acquisition files (or a previously-saved
  `.bin` file) into a FITS image sequence.

## Requirements

- Python 3.8+
- A YAML config file describing your testbed (see [Configuration](#configuration) below)

Runtime dependencies (`numpy`, `astropy`, `PyYAML`, `npTDMS`, `ipython`) are
installed automatically by pip.

`cds` additionally needs the separate `catcam` package, but only when
processing a whole directory of files at once (SERIES mode). Passing
individual filenames does not require `catcam`.

## Getting the code

Clone the repository from GitHub:

```bash
git clone https://github.com/uvex-mission/pySQUID.git
cd pySQUID
```

(If you don't have `git`, you can instead download a zip of the `main`
branch from
[github.com/uvex-mission/pySQUID](https://github.com/uvex-mission/pySQUID)
via Code → Download ZIP, and unzip it.)

## Installing into Miniconda3

pySQUID is a normal pip-installable package. The recommended way to use it
is inside its own conda environment, so it and its dependencies stay
isolated from everything else:

```bash
# 1. Create a fresh environment (any Python 3.8+ works; 3.11 shown here)
conda create -n pysquid python=3.11

# 2. Activate it
conda activate pysquid

# 3. Install pySQUID from the cloned repo (run this from the folder containing pyproject.toml)
pip install .
```

This installs pySQUID and its dependencies into the `pysquid` conda
environment and puts `pysquid`, `cds`, and `tdms` on `PATH` for as long as
that environment is active.

If you're developing pySQUID itself and want code edits to take effect
immediately without reinstalling, use an editable install instead:

```bash
pip install -e .
```

To leave the environment: `conda deactivate`. To come back to it later:
`conda activate pysquid`.

## Configuration

`pysquid` (and the `Camera` class it wraps) need a YAML config file
describing your testbed. At minimum it must define:

```yaml
OPERATOR: cshapiro   # Who are you?
TESTBED:  testbed1   # Which testbed station?
DETID:    W08D02     # CMOS detector ID
DETTYPE:  NUV        # UVEX detector type (FUV / NUV / LSS / ENG)
DETCTRL:  BB2        # Detector controller type
LEDWAVE:  530         # LED wavelength in nm
```

Additional keys (e.g. `HOST`, `PORT`, `VXTRA_HI`, `VXTRA_LO`) can be set to
override connection and detector defaults. See `pySQUID/USER.yaml` for a
complete example to copy and edit.

## Usage

Launch an interactive session connected to your camera:

```bash
pysquid path/to/your_config.yaml
```

This starts IPython with a `camera` (and `cam`) object already created and
connected — try `help(camera)` for the full list of commands, e.g.
`camera.ping()`.

CDS-subtract a set of raw CMOS frames:

```bash
cds frame1.fits frame2.fits -verbose
```

Run `cds --help` for the full set of options (stacking, dual-gain
splitting, grey-code descrambling, output type overrides, etc.).

Convert a TDMS acquisition to FITS:

```bash
tdms path/to/acquisition_basename
```

Run `tdms --help` for the full set of options (output directory, metadata
file, binary intermediate output, streaming/low-memory mode, etc.).

## License

MIT — see [LICENSE](LICENSE).
