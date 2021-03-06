from __future__ import absolute_import
from .blocks import create_dependency_graph
from dask.distributed import Client, LocalCluster
import traceback
import logging

logger = logging.getLogger(__name__)

def run_blockwise(
    total_roi,
    read_roi,
    write_roi,
    process_function,
    check_function=None,
    read_write_conflict=True,
    fit='valid',
    num_workers=None,
    processes=True,
    client=None):
    '''Run block-wise tasks with dask.

    Args:

        total_roi (`class:daisy.Roi`):

            The region of interest (ROI) of the complete volume to process.

        read_roi (`class:daisy.Roi`):

            The ROI every block needs to read data from. Will be shifted over
            the ``total_roi`` to cover the whole volume.

        write_roi (`class:daisy.Roi`):

            The ROI every block writes data from. Will be shifted over the
            ``total_roi`` to cover the whole volume.

        process_function (function):

            A function that will be called as::

                process_function(read_roi, write_roi)

            with ``read_roi`` and ``write_roi`` shifted for each block to
            process.

            The callee can assume that there are no read/write concurencies,
            i.e., at any given point in time the ``read_roi`` does not overlap
            with the ``write_roi`` of another process.

        check_function (function, optional):

            A function that will be called as::

                check_function(write_roi)

            ``write_roi`` shifted for each block to process.

            This function should return ``True`` if the block represented by
            ``write_roi`` was completed. This is used internally to avoid
            processing blocks that are already done and to check if a block was
            correctly processed.

            If a tuple of two functions is given, the first one will be called
            to check if the block needs to be run, and if so, the second after
            it was run to check if the run succeeded.

        read_write_conflict (``bool``, optional):

            Whether the read and write ROIs are conflicting, i.e., accessing
            the same resource. If set to ``False``, all blocks can run at the
            same time in parallel. In this case, providing a ``read_roi`` is
            simply a means of convenience to ensure no out-of-bound accesses
            and to avoid re-computation of it in each block.

        fit (``string``, optional):

            How to handle cases where shifting blocks by the size of
            ``block_write_roi`` does not tile the ``total_roi``. Possible
            options are:

            "valid": Skip blocks that would lie outside of ``total_roi``. This
            is the default::

                |---------------------------|     total ROI

                |rrrr|wwwwww|rrrr|                block 1
                       |rrrr|wwwwww|rrrr|         block 2
                                                  no further block

            "overhang": Add all blocks that overlap with ``total_roi``, even if
            they leave it. Client code has to take care of save access beyond
            ``total_roi`` in this case.::

                |---------------------------|     total ROI

                |rrrr|wwwwww|rrrr|                block 1
                       |rrrr|wwwwww|rrrr|         block 2
                              |rrrr|wwwwww|rrrr|  block 3 (overhanging)

            "shrink": Like "overhang", but shrink the boundary blocks' read and
            write ROIs such that they are guaranteed to lie within
            ``total_roi``. The shrinking will preserve the context, i.e., the
            difference between the read ROI and write ROI stays the same.::

                |---------------------------|     total ROI

                |rrrr|wwwwww|rrrr|                block 1
                       |rrrr|wwwwww|rrrr|         block 2
                              |rrrr|www|rrrr|     block 3 (shrunk)

        num_workers (int, optional):

            The number of parallel processes or threads to run. Only effective
            if ``client`` is ``None``.

        processes (bool, optional):

            If ``True`` (default), spawns a process per worker, otherwise a
            thread.

        client (optional):

            The dask client to submit jobs to. If ``None``, a client will be
            created from ``dask.distributed.Client`` with ``num_workers``
            workers.

    Returns:

        True, if all tasks succeeded (or were skipped because they were already
        completed in an earlier run).
    '''

    blocks = create_dependency_graph(
        total_roi,
        read_roi,
        write_roi,
        read_write_conflict,
        fit)

    if check_function is not None:

        try:
            pre_check, post_check = check_function
        except:
            pre_check = check_function
            post_check = check_function

    else:

        pre_check = lambda _: False
        post_check = lambda _: True

    # dask requires strings for task names, string representation of
    # `class:Roi` is assumed to be unique.
    tasks = {
        block_to_dask_name(block): (
            check_and_run,
            block,
            process_function,
            pre_check,
            post_check,
            [ block_to_dask_name(ups) for ups in upstream_blocks ]
        )
        for block, upstream_blocks in blocks
    }

    own_client = client is None

    if own_client:

        if num_workers is not None:
            print("Creating local cluster with %d workers..."%num_workers)

        if processes:
            cluster = LocalCluster(
                n_workers=num_workers,
                threads_per_worker=1,
                memory_limit=0,
                diagnostics_port=None)
        else:
            cluster = LocalCluster(
                n_workers=1,
                threads_per_worker=num_workers,
                processes=False,
                memory_limit=0,
                diagnostics_port=None)

        client = Client(cluster)

    logger.info("Scheduling %d tasks...", len(tasks))

    # don't show dask performance warnings (too verbose, probably not
    # applicable to our use-case)
    logging.getLogger('distributed.utils_perf').setLevel(logging.ERROR)

    # run all tasks
    results = client.get(tasks, list(tasks.keys()))

    if own_client:

        try:

            # don't show dask distributes warning during shutdown
            logging.getLogger('distributed').setLevel(logging.ERROR)

            client.close()

        # ignore exceptions during shutdown
        except Exception:
            pass

    succeeded = [ t for t, r in zip(tasks, results) if r == 1 ]
    skipped = [ t for t, r in zip(tasks, results) if r == 0 ]
    failed = [ t for t, r in zip(tasks, results) if r == -1 ]
    errored = [ t for t, r in zip(tasks, results) if r == -2 ]

    logger.info(
        "Ran %d tasks, of which %d succeeded, %d were skipped, %d failed (%d "
        "failed check, %d errored)",
        len(tasks), len(succeeded), len(skipped),
        len(failed) + len(errored), len(failed), len(errored))

    if len(failed) > 0:
        logger.info(
            "Failed blocks: %s", " ".join([str(t[1]) for _, t in failed]))

    return len(failed) + len(errored) == 0

def block_to_dask_name(block):

    return '%d'%block.block_id

def check_and_run(block, process_function, pre_check, post_check, *args):

    if pre_check(block):
        logger.info("Skipping task for block %s; already processed.", block)
        return 0

    try:
        process_function(block)
    except:
        logger.error(
            "Task for block %s failed:\n%s",
            block, traceback.format_exc())
        return -2

    if not post_check(block):
        logger.error(
            "Completion check failed for task for block %s.",
            block)
        return -1

    return 1

