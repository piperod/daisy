from __future__ import absolute_import, division
from .array import Array
from .coordinate import Coordinate
from .ext import zarr, h5py
from .roi import Roi
import fractions
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)

def _read_voxel_size_offset(ds, order='C'):

    voxel_size = None
    offset = None
    dims = None

    if 'resolution' in ds.attrs:

        voxel_size = tuple(ds.attrs['resolution'])
        dims = len(voxel_size)

    if 'offset' in ds.attrs:

        offset = tuple(ds.attrs['offset'])

        if dims is not None:
            assert dims == len(offset), (
                "resolution and offset attributes differ in length")
        else:
            dims = len(offset)

    if dims is None:
        dims = len(ds.shape)

    if voxel_size is None:
        voxel_size = (1,)*dims

    if offset is None:
        offset = (0,)*dims

    if order == 'F':
        offset = offset[::-1]
        voxel_size = voxel_size[::-1]

    return Coordinate(voxel_size), Coordinate(offset)

def open_ds(filename, ds_name, mode='r'):

    if filename.endswith('.zarr'):

        logger.debug("opening zarr dataset %s in %s", ds_name, filename)
        ds = zarr.open(filename, mode=mode)[ds_name]

        voxel_size, offset = _read_voxel_size_offset(ds, ds.order)
        roi = Roi(offset, voxel_size*ds.shape[-len(voxel_size):])

        logger.debug("opened zarr dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size)

    elif filename.endswith('.n5'):

        logger.debug("opening N5 dataset %s in %s", ds_name, filename)
        ds = zarr.open(filename, mode=mode)[ds_name]

        voxel_size, offset = _read_voxel_size_offset(ds, 'F')
        roi = Roi(offset, voxel_size*ds.shape[-len(voxel_size):])

        logger.debug("opened N5 dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size)

    elif filename.endswith('.h5') or filename.endswith('.hdf'):

        logger.debug("opening H5 dataset %s in %s", ds_name, filename)
        ds = h5py.File(filename, mode=mode)[ds_name]

        voxel_size, offset = _read_voxel_size_offset(ds, 'C')
        roi = Roi(offset, voxel_size*ds.shape[-len(voxel_size):])

        logger.debug("opened H5 dataset %s in %s", ds_name, filename)
        return Array(ds, roi, voxel_size)

    elif filename.endswith('.json'):

        logger.debug("found JSON container spec")
        with open(filename, 'r') as f:
            spec = json.load(f)

        array = open_ds(spec['container'], ds_name, mode)
        return Array(
            array.data,
            Roi(spec['offset'], spec['size']),
            array.voxel_size,
            array.roi.get_begin())

    else:

        logger.error("don't know data format of %s in %s", ds_name, filename)
        raise RuntimeError("Unknown file format for %s"%filename)

def prepare_ds(
        filename,
        ds_name,
        total_roi,
        voxel_size,
        dtype,
        write_roi=None,
        num_channels=1,
        compressor='default'):

    assert total_roi.get_shape().is_multiple_of(voxel_size), (
        "The provided ROI shape is not a multiple of voxel_size")
    assert total_roi.get_begin().is_multiple_of(voxel_size), (
        "The provided ROI offset is not a multiple of voxel_size")
    if write_roi is not None:
        assert write_roi.get_shape().is_multiple_of(voxel_size), (
            "The provided write ROI shape is not a multiple of voxel_size")

    if compressor == 'default':
        compressor = {'id': 'gzip', 'level': 5}

    ds_name = ds_name.lstrip('/')

    if filename.endswith('.h5') or filename.endswith('.hdf'):
        raise RuntimeError("prepare_ds does not support HDF5 files")
    elif filename.endswith('.zarr'):
        file_format = 'zarr'
    elif filename.endswith('.n5'):
        file_format = 'n5'
    else:
        raise RuntimeError("Unknown file format for %s"%filename)

    shape = total_roi.get_shape()/voxel_size

    if write_roi is not None:
        chunk_size = get_chunk_size(write_roi.get_shape()/voxel_size)
    else:
        chunk_size = None

    if num_channels > 1:

        shape = (num_channels,) + shape
        chunk_size = (num_channels,) + chunk_size

    if not os.path.isdir(filename):

        logger.info("Creating new %s"%filename)
        os.makedirs(filename)

        zarr.open(filename, mode='w')

    if not os.path.isdir(os.path.join(filename, ds_name)):

        logger.info("Creating new %s in %s"%(ds_name, filename))

        root = zarr.open(filename, mode='a')
        ds = root.create_dataset(
            ds_name,
            shape=shape,
            chunks=chunk_size,
            dtype=dtype,
            compressor=zarr.get_codec(compressor))

        if file_format == 'zarr':
            ds.attrs['resolution'] = voxel_size
            ds.attrs['offset'] = total_roi.get_begin()
        else:
            ds.attrs['resolution'] = voxel_size[::-1]
            ds.attrs['offset'] = total_roi.get_begin()[::-1]

        return Array(ds, total_roi, voxel_size)

    else:

        logger.debug("Trying to reuse existing dataset %s in %s..."%(ds_name, filename))
        ds = open_ds(filename, ds_name, mode='a')

        compatible = True

        if ds.shape != shape:
            logger.info("Shapes differ: %s vs %s"%(ds.shape, shape))
            compatible = False

        if ds.roi != total_roi:
            logger.info("ROIs differ: %s vs %s"%(ds.roi, total_roi))
            compatible = False

        if ds.voxel_size != voxel_size:
            logger.info("Voxel sizes differ: %s vs %s"%(ds.voxel_size, voxel_size))
            compatible = False

        if write_roi is not None and ds.data.chunks != chunk_size:
            logger.info("Chunk sizes differ: %s vs %s"%(ds.data.chunks, chunk_size))
            compatible = False

        if not compatible:

            logger.info("Existing dataset is not compatible, creating new one")

            shutil.rmtree(os.path.join(filename, ds_name))
            return prepare_ds(
                filename,
                ds_name,
                total_roi,
                voxel_size,
                dtype,
                write_roi,
                num_channels,
                compressor)

        else:

            logger.info("Reusing existing dataset")
            return ds

def get_chunk_size(block_size):
    '''Get a reasonable chunk size that divides the given block size.'''

    chunk_size = Coordinate(
        get_chunk_size_dim(b, 256)
        for b in block_size)

    logger.debug("Setting chunk size to %s", chunk_size)

    return chunk_size

def get_chunk_size_dim(b, target_chunk_size):

    best_k = None
    best_target_diff = 0

    for k in range(1, b+1):
        if ((b//k)*k)%b == 0:
            diff = abs(b//k - target_chunk_size)
            if best_k is None or diff < best_target_diff:
                best_target_diff = diff
                best_k = k

    return b//best_k
