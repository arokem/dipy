"""Tools to easily make multi voxel models"""

from functools import partial
import multiprocessing

import numpy as np
from tqdm import tqdm

from dipy.core.ndindex import ndindex
from dipy.reconst.base import ReconstFit
from dipy.reconst.quick_squash import quick_squash as _squash
from dipy.utils.parallel import paramap


def _parallel_fit_worker(vox_data, single_voxel_fit, **kwargs):
    """
    Works on a chunk of voxel data to create a list of
    single voxel fits.

    Parameters
    ----------
    vox_data : ndarray, shape (n_voxels, ...)
        The data to fit.

    single_voxel_fit : callable
        The fit function to use on each voxel.
    """
    return [single_voxel_fit(data, **kwargs) for data in vox_data]


def multi_voxel_fit(single_voxel_fit):
    """Method decorator to turn a single voxel model fit
    definition into a multi voxel model fit definition
    """

    def new_fit(self, data, *, mask=None, **kwargs):
        """Fit method for every voxel in data"""
        # If only one voxel just return a standard fit, passing through
        # the functions key-word arguments (no mask needed).
        if data.ndim == 1:
            return single_voxel_fit(self, data, **kwargs)

        # Make a mask if mask is None
        if mask is None:
            mask = np.ones(data.shape[:-1], bool)
        # Check the shape of the mask if mask is not None
        elif mask.shape != data.shape[:-1]:
            raise ValueError("mask and data shape do not match")

        # Fit data where mask is True
        fit_array = np.empty(data.shape[:-1], dtype=object)
        # Default to serial execution:
        engine = kwargs.get("engine", "serial")
        if engine == "serial":
            bar = tqdm(
                total=np.sum(mask), position=0, disable=kwargs.get("verbose", True)
            )
            bar.set_description("Fitting reconstruction model using serial execution")
            for ijk in ndindex(data.shape[:-1]):
                if mask[ijk]:
                    fit_array[ijk] = single_voxel_fit(self, data[ijk], **kwargs)
                bar.update()
            bar.close()
        else:
            data_to_fit = data[np.where(mask)]
            single_voxel_with_self = partial(single_voxel_fit, self)
            n_jobs = kwargs.get("n_jobs", multiprocessing.cpu_count() - 1)
            vox_per_chunk = kwargs.get(
                "vox_per_chunk", np.max([data_to_fit.shape[0] // n_jobs, 1])
            )
            chunks = [
                data_to_fit[ii : ii + vox_per_chunk]
                for ii in range(0, data_to_fit.shape[0], vox_per_chunk)
            ]
            parallel_kwargs = {}
            for kk in ["n_jobs", "vox_per_chunk", "engine", "verbose"]:
                if kk in kwargs:
                    parallel_kwargs[kk] = kwargs[kk]
            fit_array[np.where(mask)] = np.concatenate(
                (
                    paramap(
                        _parallel_fit_worker,
                        chunks,
                        func_args=[single_voxel_with_self],
                        func_kwargs=kwargs,
                        **parallel_kwargs,
                    )
                )
            )
        return MultiVoxelFit(self, fit_array, mask)

    return new_fit


class MultiVoxelFit(ReconstFit):
    """Holds an array of fits and allows access to their attributes and
    methods"""

    def __init__(self, model, fit_array, mask):
        self.model = model
        self.fit_array = fit_array
        self.mask = mask

    @property
    def shape(self):
        return self.fit_array.shape

    def __getattr__(self, attr):
        result = CallableArray(self.fit_array.shape, dtype=object)
        for ijk in ndindex(result.shape):
            if self.mask[ijk]:
                result[ijk] = getattr(self.fit_array[ijk], attr)
        return _squash(result, self.mask)

    def __getitem__(self, index):
        item = self.fit_array[index]
        if isinstance(item, np.ndarray):
            return MultiVoxelFit(self.model, item, self.mask[index])
        else:
            return item

    def predict(self, *args, **kwargs):
        """
        Predict for the multi-voxel object using each single-object's
        prediction API, with S0 provided from an array.
        """
        S0 = kwargs.get("S0", np.ones(self.fit_array.shape))
        idx = ndindex(self.fit_array.shape)
        ijk = next(idx)

        def gimme_S0(S0, ijk):
            if isinstance(S0, np.ndarray):
                return S0[ijk]
            else:
                return S0

        kwargs["S0"] = gimme_S0(S0, ijk)
        # If we have a mask, we might have some Nones up front, skip those:
        while self.fit_array[ijk] is None:
            ijk = next(idx)

        if not hasattr(self.fit_array[ijk], "predict"):
            msg = "This model does not have prediction implemented yet"
            raise NotImplementedError(msg)

        first_pred = self.fit_array[ijk].predict(*args, **kwargs)
        result = np.zeros(self.fit_array.shape + (first_pred.shape[-1],))
        result[ijk] = first_pred
        for ijk in idx:
            kwargs["S0"] = gimme_S0(S0, ijk)
            # If it's masked, we predict a 0:
            if self.fit_array[ijk] is None:
                result[ijk] *= 0
            else:
                result[ijk] = self.fit_array[ijk].predict(*args, **kwargs)

        return result


class CallableArray(np.ndarray):
    """An array which can be called like a function"""

    def __call__(self, *args, **kwargs):
        result = np.empty(self.shape, dtype=object)
        for ijk in ndindex(self.shape):
            item = self[ijk]
            if item is not None:
                result[ijk] = item(*args, **kwargs)
        return _squash(result)
