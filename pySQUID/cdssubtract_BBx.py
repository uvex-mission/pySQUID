#!/usr/bin/env python
# Author: Chaz Shapiro (2022)
#
# Digitally CDS subtract a CMOS MKxNK raw image acquired with UVEX BBx camera
# Output will be written to same directory as input
# 
# Assumes each extension contains 2 or 4 frames i.e. 1 or 2 CDS pairs:
#  LOW baseline | HI baseline | HI signal | LOW signal --> 4 x YSIZE x XSIZE array
#
# Multi-extension assumes all images are the same dimensions and headers are in 0th ext.
# No image in extension 0

CHANNEL_SIZE = 256 # Numnber of columns in one MKxNK readout channel
gainkey = 'GAINMODE'  # high, low, or dual
skipkey = 'NSKIP'
greykey = 'GREY'

import argparse
import gc
import os
import os.path as path
import sys

import astropy.io.fits as pf
import numpy as np

from catcam.utils import FILENAMES_FILE

stack_dict = {
    'median'    :   np.median
    ,'mean'     :   np.mean
    ,'var'      :   np.var
    ,'std'      :   np.std
    ,'sum'      :   np.sum
}

## Gray Code Descrambler
def binary_to_gray(n):
    n = int(n)
    n ^= (n>>1)
    return n

def DescramblerGrayCodeImage(image, chansize=CHANNEL_SIZE):
    ysize, xsize = image.shape
    nchan = int(xsize/chansize)
    binarycode = range(chansize)
    graycode = np.array([binary_to_gray(i) for i in binarycode])
    t = np.argsort(graycode, axis=-1, kind=None, order=None) ##
    image = np.reshape(image,(ysize,chansize,nchan), order='F')
    image = image[:,t,:]
    return image.reshape((ysize,chansize*nchan), order='F')

def stack_extensions(fname, stack_function='median', outtype='int16', chunksize=CHANNEL_SIZE):
    ''' Stack all image extentions in a FITS file, going 1 channel (column chunk) at a time to reduce memory usage.
    Assumes 1st extension is Primary with no image '''

    with pf.open(fname, mode='readonly') as hdulist:

        print('Stacking images: %i' % (len(hdulist)-1) )
        sfunc = stack_dict[stack_function]

        ysize,xsize = hdulist[-1].data.shape
        nchunk = int(xsize/chunksize)

        # Load same channel from each frame and stack frame-wise  ### BIG MEMORY USAGE HERE
        imgcube = [ sfunc([hdu.data.reshape(ysize,nchunk,chunksize)[:,i] for hdu in hdulist[1:]], axis=0) for i in range(nchunk)]

        # Recombine pieces into the shape of a single frame
        imgcube = np.swapaxes(imgcube,0,1)      # (ysize, nchunk, chunksize)
        imgcube = imgcube.reshape(ysize, -1)    # (ysize, xsize)

        # FITS file with Primary and stacked Image HDU; copy primary header to image
        hdu0 = hdulist[0].copy() # avoid reference to file about to close
        return pf.HDUList([hdu0, pf.ImageHDU(imgcube.astype(outtype), header=hdu0.header)])

def breakup_dual_file(outpath):
    ''' Break apart a processed dual gain file into high and low files
        outpath should be a processed FITS file with high and low gain side by side in each image extension
    '''

    hdulist = pf.open(outpath)
    base, ext = path.splitext(outpath)
    flist_out = []

    for dg in ['LOW','HIGH']:
        dgain = 'DUAL_'+dg
        outpath_d = base+'_'+dg[:2] + ext # basename_HI or basename_LO
        hdulist.writeto(outpath_d, overwrite=True)
        hdulist_d = pf.open(outpath_d, mode='update')

        for hdu in hdulist_d:
            # Change the header for each file type
            hdu.header[gainkey] = dgain

            try:  _ = hdu.data.shape
            except:  continue  # No data, move on (probably the primary extension)

            if dgain == 'DUAL_HIGH': hdu.data = hdu.data[0]
            if dgain == 'DUAL_LOW' : hdu.data = hdu.data[1]

        hdulist_d.flush()
        hdulist_d.close()

        flist_out.append(outpath_d)
        print(outpath_d)

    # Close and delete the combined file
    hdulist.close()
    del hdulist, hdulist_d
    gc.collect()

    return flist_out

def processCDS(args):
    ''' THE MAIN REDUCTION SCRIPT
    Input is an ArgumentParser object; all of the items listed in the create_parser() section below are REQUIRED in the object.
    Use create_parser() to make the object and all items will be there (defaults used if not specified).
    '''

    outtag = args.tag+'_'

    # Are we processing a standard SERIES in its own directory?  Needs a file with list of filenames
    if path.isdir(args.filename[0]):
        if len(args.filename)>1: sys.exit('Too many arguments; please specify 1 directory only')
        print('Processing the whole directory...')
        IMDIR = args.filename[0]
        # Look for series records
        FILENAMES = path.join(IMDIR,FILENAMES_FILE)
        if not path.isfile(FILENAMES): sys.exit('Missing file: '+FILENAMES)

        # Parse filenames
        with open(FILENAMES) as file:
            flist = [path.join(IMDIR,line.rstrip()) for line in file]

        # Override filenames with filenames list
        args.filename = flist

    if args.reference:
        refimg = pf.getdata(args.reference)

    # MAIN LOOP OVER FITS FILES
    flist_out = []
    for f in args.filename:

        # Prepend tags to filename and save in same path
        inpath,basename = path.split(f)
        if inpath=='': inpath='.'
        outpath=inpath+'/'+outtag+basename
        outpath_stack=inpath+'/'+args.stack_function+'_'+outtag+basename

        # Parse the original file
        hdulist_orig = pf.open(f, mode='readonly')  # memmap=True by default but we get errors when setting it (known astropy issue)
        hdr0 = hdulist_orig[0].header  # telemetry is in 0th FITS header
        Norig = len(hdulist_orig)
        MULTIEXT = (Norig>1)   # Is the file multi-extension?

        # Check header for Dual Gain / High Dynamic Range - unfortunately same acronym as "hdr"
        gainmode = hdr0[gainkey] if gainkey in hdr0 else ''
        if gainmode == '': print('Could not get GAIN key: %s'%f)
        DUALGAIN = (gainmode.strip().upper() in ['HDR','DUAL'])

        # Parse NSKIP
        if args.nskip is not None:
            nskip = args.nskip
        else:
            try:
                nskip = int(hdr0[skipkey])
            except:
                print('Could not get %s key:'%skipkey,f)
                nskip = 0
        hdr0['NSKIPCDS'] = nskip
        if nskip>0: 
            assert MULTIEXT
            assert len(hdulist_orig)-nskip-1 >= 1  # subtract skipped extensions and primary

        # Parse GREY
        if args.grey is not None:
            grey = args.grey
        else:
            try:
                grey = hdr0[greykey].upper().strip() in ['TRUE','T']
            except:
                print('Could not get %s key:'%greykey,f)
                grey = False
        hdr0['GREYCDS'] = grey

        # Parse stacking; Require at least 2 image extensions to stack
        STACKING = args.stack and MULTIEXT and Norig-1-nskip >= 2

        # Make empty HDU container file for the CDS subtracted frames, starting with Primary HDU
        hdulist_orig[0:1].writeto(outpath, overwrite=True) 

        # CDS SUBTRACTION LOOP
        # for ex, hdu in enumerate(hdulist_orig):
        for ex in range(Norig):

            if MULTIEXT and ex<=nskip: continue  # When skipping N frames and the primary, start with frame N+1

            img = hdulist_orig[ex].data.copy()
            if img.ndim == 2: ysize,xsize = img.shape
            elif img.ndim == 3: zsize,ysize,xsize = img.shape
            
            nchan = args.nchan if args.nchan else int(xsize/CHANNEL_SIZE)

            if xsize%CHANNEL_SIZE > 0:
                print('ERROR: Image width is not a multiple of channel size (%s)'%CHANNEL_SIZE)
                sys.exit(-1)

            # img.resize(ysize,nchan,CHANNEL_SIZE)  # Organize image by channel

            # Subtract a reference file or use only data from the image?
            if args.reference:
                refimg = pf.getdata(args.reference)
                diff = img.astype(refimg.dtype) - refimg
                outtype = refimg.dtype
                del refimg

            elif DUALGAIN: # Frame order is 0: Reset Low, 1: Reset High, 2: Signal High, 3: Signal Low
                diff = img[2:] - img[1::-1]  # HI, LO 
                outtype = img.dtype.name
                if outtype[0]=='u': outtype=outtype[1:]  # convert unsigned to signed

            else:
                diff = img[1]-img[0]
                outtype = img.dtype.name
                if outtype[0]=='u': outtype=outtype[1:]  # convert unsigned to signed

            if (grey and not args.reference): # Reference image assumed already deinterlaced
                diff = DescramblerGrayCodeImage(diff)

            # Append the CDS subtracted frame to the container file and clear memory
            if args.outtype: outtype=args.outtype
            pf.append(outpath, diff.astype(outtype), header=hdulist_orig[ex].header)
            if args.verbose and ex==(Norig-1): print('Input: %s   Output: %s'%(img.dtype.name, outtype))
            del img, diff
            gc.collect()

    #|---- still in loop over filenames

        # CDS subtraction is complete; switch to CDS subtracted file
        hdulist_orig.close()

        if STACKING:

            if args.outtype: 
                outtype = args.outtype
            elif (args.stack_function).lower() == 'median': 
                outtype = 'int16'
            else:
                outtype = 'float32'

            stackedHDUlist = stack_extensions(outpath, stack_function=args.stack_function, outtype=outtype)

            # Write output FITS file with Primary and stacked Image HDU
            stackedHDUlist.writeto(outpath_stack, overwrite=True)
            if args.verbose: print(outpath_stack)
            # flist_out.append(outpath_stack)

            del stackedHDUlist
            gc.collect()

            # Delete the unstacked file; overwrite the output filename
            os.remove(outpath)
            outpath = outpath_stack

        # Split the dual gain file (high and low gain side by side) into high and low files; delete the original
        if DUALGAIN:
            flist_out_d = breakup_dual_file(outpath)
            for f in flist_out_d: 
                flist_out.append(f)
                if args.verbose: print(f)
            os.remove(outpath)
            # print(outpath, '-- Deleted')
        else:
            flist_out.append(outpath)
            if args.verbose: print(outpath)

    return flist_out

def create_parser():
    parser = argparse.ArgumentParser(description='Digitally CDS subtract a raw image from CMOS MKxNK')
    parser.add_argument('filename', type=str, nargs='+')
    parser.add_argument('-tag', type=str, default='cds' ,help='Output filename tag; default=cds')
    parser.add_argument('-nchan' ,type=int ,default=None ,help='Override number of channels')
    parser.add_argument('-outtype', type=str, default=None ,help='Override CDS output data type')
    parser.add_argument('-reference', type=str, default=None, help='path and filename of reference frame to subtract')
    parser.add_argument('-verbose', action='store_true', default=False ,help='Print output filenames as they are written')
    parser.add_argument('-stack', action='store_true', default=False ,help='Stack frames (default=median)')
    parser.add_argument('-stack_function', type=str, default='median', choices=stack_dict.keys() ,help='Function for stacking frames; default=median')
    parser.add_argument('-nskip', type=int, default=None ,help='Discard 1st N frames')
    parser.add_argument('-grey', action='store_true', default=None ,help='Descramble rows using grey coding')
    # parser.add_argument('-chansize', type=int, default=CHANNEL_SIZE ,help='Number of columns per detector video channel')

    return parser

if __name__ == "__main__":

    parser = create_parser()
    args = parser.parse_args()

    _ = processCDS(args)

