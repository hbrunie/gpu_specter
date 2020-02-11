#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import sys
import numpy as np
import scipy.sparse
import scipy.linalg
from scipy.sparse import spdiags, issparse
from scipy.sparse.linalg import spsolve
import time
import math
from astropy.table import Table


from numpy.polynomial import hermite_e as He

import numba
import cupy as cp
import cupyx as cpx
import cupyx.scipy.special
from numba import cuda

def native_endian(data):
    """Temporary function, sourced from desispec.io
    Convert numpy array data to native endianness if needed.
    Returns new array if endianness is swapped, otherwise returns input data
    Context:
    By default, FITS data from astropy.io.fits.getdata() are not Intel
    native endianness and scipy 0.14 sparse matrices have a bug with
    non-native endian data.
    """
    if data.dtype.isnative:
        return data
    else:
        return data.byteswap().newbyteorder()


@cuda.jit
def legvander(x, deg, output_matrix):
    i = cuda.grid(1)
    stride = cuda.gridsize(1)
    for i in range(i, x.shape[0], stride):
        output_matrix[i][0] = 1
        output_matrix[i][1] = x[i]
        for j in range(2, deg + 1):
            output_matrix[i][j] = (output_matrix[i][j-1]*x[i]*(2*j - 1) - output_matrix[i][j-2]*(j - 1)) / j

def legvander_wrapper(x, deg):
    """Temporary wrapper that allocates memory and defines grid before calling legvander.
    Probably won't be needed once cupy has the correpsponding legvander function.
    Input: Same as cpu version of legvander
    Output: legvander matrix, cp.ndarray
    """
    output = cp.ndarray((len(x), deg + 1))
    blocksize = 256
    numblocks = (len(x) + blocksize - 1) // blocksize
    legvander[numblocks, blocksize](x, deg, output)
    return output

def evalcoeffs(wavelengths, psfdata):
    '''
    wavelengths: 1D array of wavelengths to evaluate all coefficients for all wavelengths of all spectra
    psfdata: Table of parameter data ready from a GaussHermite format PSF file
    Returns a dictionary params[paramname] = value[nspec, nwave]
    The Gauss Hermite coefficients are treated differently:
        params['GH'] = value[i,j,nspec,nwave]
    The dictionary also contains scalars with the recommended spot size HSIZEX, HSIZEY
    and Gauss-Hermite degrees GHDEGX, GHDEGY (which is also derivable from the dimensions
    of params['GH'])
    '''
    # Initialization
    wavemin, wavemax = psfdata['WAVEMIN'][0], psfdata['WAVEMAX'][0]
    wx = (wavelengths - wavemin) * (2.0 / (wavemax - wavemin)) - 1.0

    L = legvander_wrapper(wx, psfdata.meta['LEGDEG'])
    p = dict(WAVE=wavelengths) # p doesn't live on the gpu, but it's last-level values do
    nparam, nspec, ndeg = psfdata['COEFF'].shape
    nwave = L.shape[0]

    # Init zeros
    p['GH'] = cp.zeros((psfdata.meta['GHDEGX']+1, psfdata.meta['GHDEGY']+1, nspec, nwave))
    # Init gpu coeff
    coeff_gpu = cp.array(native_endian(psfdata['COEFF']))

    k = 0
    for name, coeff in zip(psfdata['PARAM'], psfdata['COEFF']):
        name = name.strip()
        if name.startswith('GH-'):
            i, j = map(int, name.split('-')[1:3])
            p['GH'][i,j] = L.dot(coeff_gpu[k].T).T
        else:
            p[name] = L.dot(coeff_gpu[k].T).T
        k += 1

    #- Include some additional keywords that we'll need
    for key in ['HSIZEX', 'HSIZEY', 'GHDEGX', 'GHDEGY']:
        p[key] = psfdata.meta[key]

    return p


@cuda.jit
def hermevander(x, deg, output_matrix):
    i = cuda.blockIdx.x
    _, j = cuda.grid(2)
    _, stride = cuda.gridsize(2)
    for j in range(j, x.shape[1], stride):
        output_matrix[i][j][0] = 1
        if deg > 0:
            output_matrix[i][j][1] = x[i][j]
            for k in range(2, deg + 1):
                output_matrix[i][j][k] = output_matrix[i][j][k-1]*x[i][j] - output_matrix[i][j][k-2]*(k-1)

def hermevander_wrapper(x, deg):
    """Temprorary wrapper that allocates memory and calls hermevander_gpu
    """
    if x.ndim == 1:
        x = cp.expand_dims(x, 0)
    output = cp.ndarray(x.shape + (deg+1,))
    blocksize = 256
    numblocks = (x.shape[0], (x.shape[1] + blocksize - 1) // blocksize)
    hermevander[numblocks, blocksize](x, deg, output)
    return cp.squeeze(output)


def calc_pgh(ispec, wavelengths, psfparams):
    '''
    Calculate the pixelated Gauss Hermite for all wavelengths of a single spectrum
    ispec : integer spectrum number
    wavelengths : array of wavelengths to evaluate
    psfparams : dictionary of PSF parameters returned by evalcoeffs
    returns pGHx, pGHy
    where pGHx[ghdeg+1, nwave, nbinsx] contains the pixel-integrated Gauss-Hermite polynomial
    for all degrees at all wavelengths across nbinsx bins spaning the PSF spot, and similarly
    for pGHy.  The core PSF will then be evaluated as
    PSFcore = sum_ij c_ij outer(pGHy[j], pGHx[i])
    '''

    #- shorthand
    p = psfparams

    #- spot size (ny,nx)
    nx = p['HSIZEX']
    ny = p['HSIZEY']
    nwave = len(wavelengths)
    p['X'], p['Y'], p['GHSIGX'], p['GHSIGY'] = \
    cp.array(p['X']), cp.array(p['Y']), cp.array(p['GHSIGX']), cp.array(p['GHSIGY'])
    xedges = cp.repeat(cp.arange(nx+1) - nx//2, nwave).reshape(nx+1, nwave)
    yedges = cp.repeat(cp.arange(ny+1) - ny//2, nwave).reshape(ny+1, nwave)

    #- Shift to be relative to the PSF center at 0 and normalize
    #- by the PSF sigma (GHSIGX, GHSIGY)
    #- xedges[nx+1, nwave]
    #- yedges[ny+1, nwave]
    xedges = (xedges - p['X'][ispec]%1)/p['GHSIGX'][ispec]
    yedges = (yedges - p['Y'][ispec]%1)/p['GHSIGY'][ispec]

    #- Degree of the Gauss-Hermite polynomials
    ghdegx = p['GHDEGX']
    ghdegy = p['GHDEGY']

    #- Evaluate the Hermite polynomials at the pixel edges
    #- HVx[ghdegx+1, nwave, nx+1]
    #- HVy[ghdegy+1, nwave, ny+1]
    HVx = hermevander_wrapper(xedges, ghdegx).T
    HVy = hermevander_wrapper(yedges, ghdegy).T

    #- Evaluate the Gaussians at the pixel edges
    #- Gx[nwave, nx+1]
    #- Gy[nwave, ny+1]
    Gx = cp.exp(-0.5*xedges**2).T / cp.sqrt(2. * cp.pi)
    Gy = cp.exp(-0.5*yedges**2).T / cp.sqrt(2. * cp.pi)

    #- Combine into Gauss*Hermite
    GHx = HVx * Gx
    GHy = HVy * Gy

    #- Integrate over the pixels using the relationship
    #  Integral{ H_k(x) exp(-0.5 x^2) dx} = -H_{k-1}(x) exp(-0.5 x^2) + const

    #- pGHx[ghdegx+1, nwave, nx]
    #- pGHy[ghdegy+1, nwave, ny]
    pGHx = cp.zeros((ghdegx+1, nwave, nx))
    pGHy = cp.zeros((ghdegy+1, nwave, ny))
    pGHx[0] = 0.5 * cp.diff(cupyx.scipy.special.erf(xedges/cp.sqrt(2.)).T)
    pGHy[0] = 0.5 * cp.diff(cupyx.scipy.special.erf(yedges/cp.sqrt(2.)).T)
    pGHx[1:] = GHx[:ghdegx,:,0:nx] - GHx[:ghdegx,:,1:nx+1]
    pGHy[1:] = GHy[:ghdegy,:,0:ny] - GHy[:ghdegy,:,1:ny+1]

    return pGHx, pGHy

#have to preallocate spots
@cuda.jit()
def multispot(pGHx, pGHy, ghc, mspots):
    '''
    TODO: Document
    '''
    nx = pGHx.shape[-1]
    ny = pGHy.shape[-1]
    nwave = pGHx.shape[1]

    #this is the magic step
    iwave = cuda.grid(1)

    n = pGHx.shape[0]
    m = pGHy.shape[0]

    if (0 <= iwave < nwave):
    #yanked out the i and j loops in lieu of the cuda grid of threads
        for i in range(pGHx.shape[0]):
            px = pGHx[i,iwave]
            for j in range(0, pGHy.shape[0]):
                py = pGHy[j,iwave]
                c = ghc[i,j,iwave]

                for iy in range(len(py)):
                    for ix in range(len(px)):
                        mspots[iwave, iy, ix] += c * py[iy] * px[ix]

#no numba for now, contains cupy and some cpu code still
def cache_spots(nx, ny, nspec, nwave, p, wavelengths):
    spots = cp.zeros((nspec, nwave, ny, nx))
    mspots = cp.zeros((nwave, ny, nx))
    #gpu stuff (for v100, total number of threads per multiprocessor = 2048)
    #max threads per block is 1024
    #this is a 1d kernel for multispot
    threads_per_block = 64
    blocks_per_grid = 4
    for ispec in range(nspec):
        pGHx, pGHy = calc_pgh(ispec, wavelengths, p)
        ghc = p['GH'][:,:,ispec,:]
        multispot[blocks_per_grid, threads_per_block](pGHx, pGHy, ghc, mspots)
        spots[ispec] = mspots
    return spots.get() #for now return spots on the cpu

#lets go for it and try to get projection_matrix on the gpu

@cuda.jit()
def projection_matrix(A, xc, yc, xmin, ymin, ispec, iwave, nspec, nwave, spots):
    ny, nx = spots.shape[2:4]
    #this is the magic step
    i = cuda.grid(1) #loop over nspec
    #only do 1d to avoid a datarace in 2d
    if (0 <= i < nspec):
        for j in range(nwave):
            ixc = xc[ispec+i, iwave+j] - xmin
            iyc = yc[ispec+i, iwave+j] - ymin
            #A[iyc:iyc+ny, ixc:ixc+nx, i, j] = spots[ispec+i,iwave+j]
            local_spot = spots[ispec+i,iwave+j]
            A = local_spot            

@cuda.jit()
def projection_matrix2(A, xc, yc, xmin, ymin, ispec, iwave, nspec, nwave, spots):
    #this is the heart of the projection matrix calculation
    ny, nx = spots.shape[2:4]
    i, j = cuda.grid(2)
    #no loops, just a boundary check
    if (0 <= i < nspec) and (0 <= j <nwave):
        ixc = xc[ispec+i, iwave+j] - xmin
        iyc = yc[ispec+i, iwave+j] - ymin
        #A[iyc:iyc+ny, ixc:ixc+nx, i, j] = spots[ispec+i,iwave+j]
        #this fancy indexing is not allowed in numba gpu (although it is in numba cpu...)
        #try this instead
        for iy, y in enumerate(range(iyc,iyc+ny)):
            for ix, x in enumerate(range(ixc,ixc+nx)):
                temp_spot = spots[ispec+i, iwave+j][iy, ix]
                A[y, x, i, j] += temp_spot

#- Read the PSF parameters from a PSF file without using specter
psfdata = Table.read('psf.fits')

#- Generate some fake input data
wavemin, wavemax = 6000., 6050.
wavelengths = np.arange(wavemin, wavemax)
nwave = len(wavelengths)
nspec = 5
influx = np.zeros((nspec, nwave))
for i in range(nspec):
    influx[i, 5*(i+1)] = 100*(i+1)

#first function, contains legvander
p = evalcoeffs(wavelengths, psfdata)

nx = p['HSIZEX']
ny = p['HSIZEY']

xc = np.floor(p['X'] - p['HSIZEX']//2).astype(int)
yc = np.floor(p['Y'] - p['HSIZEY']//2).astype(int)
corners = (xc, yc)

#preallocate
spots = np.zeros((nspec, nwave, ny, nx))
mspots = np.zeros((nwave, ny, nx))

#gpu stuff (for v100, total number of threads per multiprocessor = 2048)
#max threads per block is 1024

#this is a 1d kernel for multispot in cache_spots
threads_per_block = 64
blocks_per_grid = 4

#call cache_spots here
spots = cache_spots(nx, ny, nspec, nwave, p, wavelengths)

#def projection_matrix(ispec, nspec, iwave, nwave, spots, corners):
#last function, parent function to all others
#resides inside of ex2d_patch for now
ny, nx = spots.shape[2:4]
xc, yc = corners
#for now
ispec = 0
iwave = 0
#just do this before we get to gpu land (for now)
xmin = np.min(xc[ispec:ispec+nspec, iwave:iwave+nwave].get())
xmax = np.max(xc[ispec:ispec+nspec, iwave:iwave+nwave].get()) + nx
ymin = np.min(yc[ispec:ispec+nspec, iwave:iwave+nwave].get())
ymax = np.max(yc[ispec:ispec+nspec, iwave:iwave+nwave].get()) + ny
A = np.zeros((ymax-ymin,xmax-xmin,nspec,nwave), dtype=np.float64)

#this is a 2d kernel for projection matrix
threads_per_block = (16,16) #needs to be 2d!
#copy from matt who copied from cuda docs so it's probably legit
blocks_per_grid_x = math.ceil(A.shape[0] / threads_per_block[0])
blocks_per_grid_y = math.ceil(A.shape[1] / threads_per_block[1])
blocks_per_grid = (blocks_per_grid_x, blocks_per_grid_y)

#gpu 1
A = np.zeros((ymax-ymin,xmax-xmin,nspec,nwave), dtype=np.float64)
xc, yc = corners
projection_matrix[blocks_per_grid, threads_per_block](A, xc, yc, xmin, ymin, ispec, iwave, nspec, nwave, spots)

A_gpu = A
print("np.max(A_gpu)", np.max(A_gpu))

#gpu 2

#def projection_matrix(ispec, nspec, iwave, nwave, spots, corners):
#last function, parent function to all others
#resides inside of ex2d_patch for now
ny, nx = spots.shape[2:4]
xc, yc = corners
#for now
ispec = 0
iwave = 0
#just do this before we get to gpu land (for now)
xmin = np.min(xc[ispec:ispec+nspec, iwave:iwave+nwave].get())
xmax = np.max(xc[ispec:ispec+nspec, iwave:iwave+nwave].get()) + nx
ymin = np.min(yc[ispec:ispec+nspec, iwave:iwave+nwave].get())
ymax = np.max(yc[ispec:ispec+nspec, iwave:iwave+nwave].get()) + ny
A = np.zeros((ymax-ymin,xmax-xmin,nspec,nwave), dtype=np.float64)

#this is a 2d kernel for projection matrix
threads_per_block = (16,16) #needs to be 2d!
#copy from matt who copied from cuda docs so it's probably legit
blocks_per_grid_x = math.ceil(A.shape[0] / threads_per_block[0])
blocks_per_grid_y = math.ceil(A.shape[1] / threads_per_block[1])
blocks_per_grid = (blocks_per_grid_x, blocks_per_grid_y)

#gpu 1
A = np.zeros((ymax-ymin,xmax-xmin,nspec,nwave), dtype=np.float64)
xc, yc = corners
projection_matrix2[blocks_per_grid, threads_per_block](A, xc, yc, xmin, ymin, ispec, iwave, nspec, nwave, spots)

A_gpu2 = A
print("np.max(A_gpu2)", np.max(A_gpu2))

#fails, probably due to data race in 2d kernel
#assert np.allclose(A_gpu, A_gpu2)

np.save('A_gpu.npy', A_gpu)
np.save('A_gpu2.npy', A_gpu2)

np.save('gpu_spots.npy', spots)
