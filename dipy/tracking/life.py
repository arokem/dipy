"""
This is an implementation of the Linear Fascicle Evaluation (LiFE) algorithm
described in:

Pestilli, F., Yeatman, J, Rokem, A. Kay, K. and Wandell B.A. (2014). Validation
and statistical inference in living connectomes. Nature Methods 11:
1058-1063. doi:10.1038/nmeth.3098
"""
import os.path as op
import tempfile
import numpy as np
import scipy.sparse as sps
import scipy.linalg as la
import scipy.spatial.distance as dist

from dipy.reconst.base import ReconstModel, ReconstFit
from dipy.utils.six.moves import range
from dipy.tracking.utils import unique_rows
from dipy.tracking.streamline import transform_streamlines
from dipy.tracking.vox2track import _voxel2streamline
import dipy.data as dpd
import dipy.core.optimize as opt


def gradient(f):
    """
    Return the gradient of an N-dimensional array.

    The gradient is computed using central differences in the interior
    and first differences at the boundaries. The returned gradient hence has
    the same shape as the input array.

    Parameters
    ----------
    f : array_like
      An N-dimensional array containing samples of a scalar function.

    Returns
    -------
    gradient : ndarray
      N arrays of the same shape as `f` giving the derivative of `f` with
      respect to each dimension.

    Examples
    --------
    >>> x = np.array([1, 2, 4, 7, 11, 16], dtype=np.float)
    >>> gradient(x)
    array([ 1. ,  1.5,  2.5,  3.5,  4.5,  5. ])

    >>> gradient(np.array([[1, 2, 6], [3, 4, 5]], dtype=np.float))
    [array([[ 2.,  2., -1.],
           [ 2.,  2., -1.]]), array([[ 1. ,  2.5,  4. ],
           [ 1. ,  1. ,  1. ]])]

    Note
    ----
    This is a simplified implementation of gradient that is part of numpy
    1.8. In order to mitigate the effects of changes added to this
    implementation in version 1.9 of numpy, we include this implementation
    here.
    """
    f = np.asanyarray(f)
    N = len(f.shape)  # number of dimensions
    dx = [1.0]*N

    # use central differences on interior and first differences on endpoints
    outvals = []

    # create slice objects --- initially all are [:, :, ..., :]
    slice1 = [slice(None)]*N
    slice2 = [slice(None)]*N
    slice3 = [slice(None)]*N

    for axis in range(N):
        # select out appropriate parts for this dimension
        out = np.empty_like(f)
        slice1[axis] = slice(1, -1)
        slice2[axis] = slice(2, None)
        slice3[axis] = slice(None, -2)
        # 1D equivalent -- out[1:-1] = (f[2:] - f[:-2])/2.0
        out[slice1] = (f[slice2] - f[slice3])/2.0
        slice1[axis] = 0
        slice2[axis] = 1
        slice3[axis] = 0
        # 1D equivalent -- out[0] = (f[1] - f[0])
        out[slice1] = (f[slice2] - f[slice3])
        slice1[axis] = -1
        slice2[axis] = -1
        slice3[axis] = -2
        # 1D equivalent -- out[-1] = (f[-1] - f[-2])
        out[slice1] = (f[slice2] - f[slice3])

        # divide by step size
        outvals.append(out / dx[axis])
        # reset the slice object in this dimension to ":"
        slice1[axis] = slice(None)
        slice2[axis] = slice(None)
        slice3[axis] = slice(None)

    if N == 1:
        return outvals[0]
    else:
        return outvals


def streamline_gradients(streamline):
    """
    Calculate the gradients of the streamline along the spatial dimension

    Parameters
    ----------
    streamline : array-like of shape (n, 3)
        The 3d coordinates of a single streamline

    Returns
    -------
    Array of shape (3, n): Spatial gradients along the length of the
    streamline.

    """
    return np.array(gradient(np.asarray(streamline))[0])


def grad_tensor(grad, evals):
    """
    Calculate the 3 by 3 tensor for a given spatial gradient, given a canonical
    tensor shape (also as a 3 by 3), pointing at [1,0,0]

    Parameters
    ----------
    grad : 1d array of shape (3,)
        The spatial gradient (e.g between two nodes of a streamline).

    evals: 1d array of shape (3,)
        The eigenvalues of a canonical tensor to be used as a response
        function.

    """
    # This is the rotation matrix from [1, 0, 0] to this gradient of the sl:
    R = la.svd(np.matrix(grad), overwrite_a=True)[2]
    # This is the 3 by 3 tensor after rotation:
    T = np.dot(np.dot(R, np.diag(evals)), R.T)
    return T


def streamline_tensors(streamline, evals=[0.001, 0, 0]):
    """
    The tensors generated by this fiber.

    Parameters
    ----------
    streamline : array-like of shape (n, 3)
        The 3d coordinates of a single streamline

    evals : iterable with three entries
        The estimated eigenvalues of a single fiber tensor.
        (default: [0.001, 0, 0]).

    Returns
    -------
    An n_nodes by 3 by 3 array with the tensor for each node in the fiber.

    Note
    ----
    Estimates of the radial/axial diffusivities may rely on
    empirical measurements (for example, the AD in the Corpus Callosum), or
    may be based on a biophysical model of some kind.
    """

    grad = streamline_gradients(streamline)

    # Preallocate:
    tensors = np.empty((grad.shape[0], 3, 3))

    for grad_idx, this_grad in enumerate(grad):
        tensors[grad_idx] = grad_tensor(this_grad, evals)
    return tensors


def streamline_signal(streamline, gtab, evals=[0.001, 0, 0]):
    """
    The signal from a single streamline estimate along each of its nodes.

    Parameters
    ----------
    streamline : a single streamline

    gtab : GradientTable class instance

    evals : list of length 3 (optional. Default: [0.001, 0, 0])
        The eigenvalues of the canonical tensor used as an estimate of the
        signal generated by each node of the streamline.
    """
    # Gotta have those tensors:
    tensors = streamline_tensors(streamline, evals)
    sig = np.empty((len(streamline), np.sum(~gtab.b0s_mask)))
    # Extract them once:
    bvecs = gtab.bvecs[~gtab.b0s_mask]
    bvals = gtab.bvals[~gtab.b0s_mask]
    for ii, tensor in enumerate(tensors):
        ADC = np.diag(np.dot(np.dot(bvecs, tensor), bvecs.T))
        # Use the Stejskal-Tanner equation with the ADC as input, and S0 = 1:
        sig[ii] = np.exp(-bvals * ADC)
    return sig - np.mean(sig)


class LifeSignalMaker(object):
    """
    A class for generating signals from streamlines in an efficient and speedy
    manner.
    """
    def __init__(self, gtab, evals=[0.001, 0, 0], sphere=None):
        """
        Initialize a signal maker

        Parameters
        ----------
        gtab : GradientTable class instance
            The gradient table on which the signal is calculated.
        evals : list of 3 items
            The eigenvalues of the canonical tensor to use in calculating the
            signal.
        """
        if sphere is None:
            sphere = dpd.get_sphere('symmetric724')
        self.sphere = sphere
        self.gtab = gtab
        self.evals = evals
        # Initialize an empty dict to fill with signals for each of the sphere
        # vertices:
        self.signal = np.empty((self.sphere.vertices.shape[0],
                                np.sum(~gtab.b0s_mask)))
        # We'll need to keep track of what we've already calculated:
        self._calculated = []

    def calc_signal(self, xyz):
        idx = self.sphere.find_closest(xyz)
        if idx not in self._calculated:
            bvecs = self.gtab.bvecs[~self.gtab.b0s_mask]
            bvals = self.gtab.bvals[~self.gtab.b0s_mask]
            tensor = grad_tensor(self.sphere.vertices[idx], self.evals)
            ADC = np.diag(np.dot(np.dot(bvecs, tensor), bvecs.T))
            sig = np.exp(-bvals * ADC)
            sig = sig - np.mean(sig)
            self.signal[idx] = sig
            self._calculated.append(idx)

        return self.signal[idx]

    def streamline_signal(self, streamline, node=None):
        """
        Approximate the signal for a given streamline
        """
        if node is None:
            grad = streamline_gradients(streamline)
            sig_out = np.zeros((grad.shape[0], self.signal.shape[-1]))
            for ii, g in enumerate(grad):
                sig_out[ii] = self.calc_signal(g)
        else:
            if node == 0:
                g = streamline_gradients(streamline[:2])[0]
            elif node == streamline.shape[0]:
                g = streamline_gradients(streamline[-2:])[1]
            else:
                g = streamline_gradients(streamline[node - 1:node + 1])[1]
            sig_out = self.calc_signal(g)
        return sig_out


class FiberModel(ReconstModel):
    """
    A class for representing and solving predictive models based on
    tractography solutions.

    Notes
    -----
    This is an implementation of the LiFE model described in [1]_

    [1] Pestilli, F., Yeatman, J, Rokem, A. Kay, K. and Wandell
        B.A. (2014). Validation and statistical inference in living
        connectomes. Nature Methods.
    """
    def __init__(self, gtab):
        """
        Parameters
        ----------
        gtab : a GradientTable class instance

        """
        # Initialize the super-class:
        ReconstModel.__init__(self, gtab)

    def fit(self, data, streamline, affine=None,
            evals=[0.001, 0, 0], sphere=None):
        """
        Set up the necessary components for the LiFE model: the matrix of
        fiber-contributions to the DWI signal, and the coordinates of voxels
        for which the equations will be solved

        Parameters
        ----------
        streamline : list
            Streamlines, each is an array of shape (n, 3)
        affine : 4 by 4 array
            Mapping from the streamline coordinates to the data
        evals : list (3 items, optional)
            The eigenvalues of the canonical tensor used as a response
            function. Default:[0.001, 0, 0].
        sphere: `dipy.core.Sphere` instance.
            Whether to approximate (and cache) the signal on a discrete
            sphere. This may confer a significant speed-up in setting up the
            problem, but is not as accurate. If `False`, we use the exact
            gradients along the streamlines to calculate the matrix, instead of
            an approximation. Defaults to use the 724-vertex symmetric sphere
            from :mod:`dipy.data`
        """

        if affine is None:
            affine = np.eye(4)

        if sphere is None:
            sphere = dpd.get_sphere('symmetric724')

        SignalMaker = LifeSignalMaker(self.gtab,
                                      evals=evals,
                                      sphere=sphere)
        if affine is None:
            affine = np.eye(4)
        streamline = transform_streamlines(streamline, affine)
        n_nodes = np.array([s.shape[0] for s in streamline])
        cat_streamline = np.concatenate(streamline)
        sum_nodes = cat_streamline.shape[0]
        vox_coords = unique_rows(np.round(cat_streamline).astype(np.intp))

        (to_fit, weighted_signal, b0_signal, relative_signal, mean_sig,
         vox_data) = self._signals(data, vox_coords)

        # We only consider the diffusion-weighted signals in fitting:
        n_bvecs = self.gtab.bvals[~self.gtab.b0s_mask].shape[0]
        f_matrix_shape = (to_fit.shape[0], len(streamline))
        beta = np.zeros(f_matrix_shape[-1])
        gradient = np.zeros(beta.shape)
        range_bvecs = np.arange(n_bvecs).astype(int)

        # Optimization related stuff:
        iteration = 0
        ss_residuals_min = np.inf
        check_error_iter = 10
        converge_on_sse = 0.99
        sse_best = np.inf
        max_error_checks = 10
        error_checks = 0  # How many error checks have we done so far
        step_size = 0.01
        y_hat = np.zeros(to_fit.shape)
        se = (y_hat - to_fit) ** 2
        sse_by_vox = np.sum(se.reshape(vox_coords.shape[0], -1), -1)
        vox_by_sse = np.argsort(sse_by_vox)[::-1]  # From largest to smallest
        while 1:
            for v_idx in range(vox_coords.shape[0]):
                mat_row_idx = (range_bvecs + v_idx * n_bvecs).astype(np.intp)
                # For each fiber in that voxel:
                s_in_vox = []
                for sl_idx, s in enumerate(streamline):
                    v_s_dist = dist.cdist(np.round(s).astype(np.intp),
                                          np.array([vox_coords[v_idx]]))
                    nodes_in_vox = np.where(v_s_dist == 0)[0]
                    if len(nodes_in_vox) > 0:
                        s_in_vox.append((sl_idx, s, nodes_in_vox))
                f_matrix_row = np.zeros(len(s_in_vox) * n_bvecs)
                f_matrix_col = np.zeros(len(s_in_vox) * n_bvecs)
                f_matrix_sig = np.zeros(len(s_in_vox) * n_bvecs)
                for ii, (sl_idx, ss, nodes_in_vox) in enumerate(s_in_vox):
                    f_matrix_row[ii*n_bvecs:ii*n_bvecs+n_bvecs] = mat_row_idx
                    f_matrix_col[ii*n_bvecs:ii*n_bvecs+n_bvecs] = sl_idx
                    vox_fib_sig = np.zeros(n_bvecs)
                    for node_idx in nodes_in_vox:
                        this_signal = \
                             SignalMaker.streamline_signal(ss,
                                                           node=node_idx)
                        # Sum the signal from each node of the fiber in that
                        # voxel:
                        vox_fib_sig += this_signal
                    # And add the summed thing into the corresponding rows:
                    f_matrix_sig[ii*n_bvecs:ii*n_bvecs+n_bvecs] += vox_fib_sig

                life_matrix = sps.csr_matrix((f_matrix_sig,
                                              [f_matrix_row, f_matrix_col]),
                                             shape=f_matrix_shape)
                if iteration > 1 and (np.mod(iteration, check_error_iter) == 0):
                    y_hat[mat_row_idx] = opt.spdot(life_matrix,
                                                   beta)[mat_row_idx]
                else:
                    gradient = gradient + opt.spdot(life_matrix.T,
                                                    opt.spdot(life_matrix,
                                                            beta) - to_fit)

            if iteration > 1 and (np.mod(iteration, check_error_iter) == 0):
                sse = np.sum((to_fit - y_hat) ** 2)
                # Did we do better this time around?
                if sse < ss_residuals_min:
                    # Update your expectations about the minimum error:
                    ss_residuals_min = sse
                    beta_best = beta
                    # Are we generally (over iterations) converging?
                    if sse < sse_best:
                        sse_best = sse
                        count_bad = 0
                    else:
                        count_bad += 1
                else:
                    count_bad += 1
                if count_bad >= max_error_checks:
                    return FiberFit(self,
                                    life_matrix,
                                    vox_coords,
                                    to_fit,
                                    beta_best,
                                    weighted_signal,
                                    b0_signal,
                                    relative_signal,
                                    mean_sig,
                                    vox_data,
                                    streamline,
                                    affine,
                                    evals)
                error_checks += 1
            else:
                beta = beta - step_size * gradient
                # Set negative values to 0 (non-negative!)
                beta[beta < 0] = 0
            iteration += 1


    def _signals(self, data, vox_coords):
        """
        Helper function to extract and separate all the signals we need to fit
        and evaluate a fit of this model

        Parameters
        ----------
        data : 4D array

        vox_coords: n by 3 array
            The coordinates into the data array of the fiber nodes.
        """
        # Fitting is done on the S0-normalized-and-demeaned diffusion-weighted
        # signal:
        idx_tuple = (vox_coords[:, 0], vox_coords[:, 1], vox_coords[:, 2])
        # We'll look at a 2D array, extracting the data from the voxels:
        vox_data = data[idx_tuple]
        weighted_signal = vox_data[:, ~self.gtab.b0s_mask]
        b0_signal = np.mean(vox_data[:, self.gtab.b0s_mask], -1)
        relative_signal = (weighted_signal/b0_signal[:, None])

        # The mean of the relative signal across directions in each voxel:
        mean_sig = np.mean(relative_signal, -1)
        to_fit = (relative_signal - mean_sig[:, None]).ravel()
        return (to_fit, weighted_signal, b0_signal, relative_signal, mean_sig,
                vox_data)


class FiberFit(ReconstFit):
    """
    A fit of the LiFE model to diffusion data
    """
    def __init__(self, fiber_model, life_matrix, vox_coords, to_fit, beta,
                 weighted_signal, b0_signal, relative_signal, mean_sig,
                 vox_data, streamline, affine, evals):
        """
        Parameters
        ----------
        fiber_model : A FiberModel class instance

        params : the parameters derived from a fit of the model to the data.

        """
        ReconstFit.__init__(self, fiber_model, vox_data)

        self.vox_coords = vox_coords
        self.fit_data = to_fit
        self.beta = beta
        self.weighted_signal = weighted_signal
        self.b0_signal = b0_signal
        self.relative_signal = relative_signal
        self.mean_signal = mean_sig
        self.streamline = streamline
        self.affine = affine
        self.evals = evals

    def predict(self, gtab=None, S0=None, sphere=None):
        """
        Predict the signal

        Parameters
        ----------
        gtab : GradientTable
            Default: use self.gtab
        S0 : float or array
            The non-diffusion-weighted signal in the voxels for which a
            prediction is made. Default: use self.b0_signal

        Returns
        -------
        prediction : ndarray of shape (voxels, bvecs)
            An array with a prediction of the signal in each voxel/direction
        """
        # We generate the prediction and in each voxel, we add the
        # offset, according to the isotropic part of the signal, which was
        # removed prior to fitting:
        if gtab is None:
            gtab = self.model.gtab

        if sphere is None:
            sphere = dpd.get_sphere('symmetric724')

        SignalMaker = LifeSignalMaker(gtab,
                                      evals=self.evals,
                                      sphere=sphere)

        n_bvecs = gtab.bvals[~gtab.b0s_mask].shape[0]
        f_matrix_shape = (self.fit_data.shape[0], len(self.streamline))
        range_bvecs = np.arange(n_bvecs).astype(int)
        pred_weighted = np.zeros(self.fit_data.shape)

        for v_idx in range(self.vox_coords.shape[0]):
            mat_row_idx = (range_bvecs + v_idx * n_bvecs).astype(np.intp)
            # For each fiber in that voxel:
            s_in_vox = []
            for sl_idx, s in enumerate(self.streamline):
                v_s_dist = dist.cdist(np.round(s).astype(np.intp),
                                      np.array([self.vox_coords[v_idx]]))
                nodes_in_vox = np.where(v_s_dist == 0)[0]
                if len(nodes_in_vox) > 0:
                    s_in_vox.append((sl_idx, s, nodes_in_vox))
            f_matrix_row = np.zeros(len(s_in_vox) * n_bvecs)
            f_matrix_col = np.zeros(len(s_in_vox) * n_bvecs)
            f_matrix_sig = np.zeros(len(s_in_vox) * n_bvecs)
            for ii, (sl_idx, ss, nodes_in_vox) in enumerate(s_in_vox):
                f_matrix_row[ii*n_bvecs:ii*n_bvecs+n_bvecs] = mat_row_idx
                f_matrix_col[ii*n_bvecs:ii*n_bvecs+n_bvecs] = sl_idx
                vox_fib_sig = np.zeros(n_bvecs)
                for node_idx in nodes_in_vox:
                    this_signal = \
                         SignalMaker.streamline_signal(ss,
                                                       node=node_idx)
                    # Sum the signal from each node of the fiber in that
                    # voxel:
                    vox_fib_sig += this_signal
                # And add the summed thing into the corresponding rows:
                f_matrix_sig[ii*n_bvecs:ii*n_bvecs+n_bvecs] += vox_fib_sig

            life_matrix = sps.csr_matrix((f_matrix_sig,
                                          [f_matrix_row, f_matrix_col]),
                                         shape=f_matrix_shape)

            pred_weighted[mat_row_idx] = opt.spdot(life_matrix,
                                                   self.beta)[mat_row_idx]

        pred = np.empty((self.vox_coords.shape[0], gtab.bvals.shape[0]))
        pred[..., ~gtab.b0s_mask] = pred_weighted.reshape(
                                            pred[..., ~gtab.b0s_mask].shape)
        if S0 is None:
            S0 = self.b0_signal

        pred[..., gtab.b0s_mask] = S0[:, None]
        pred[..., ~gtab.b0s_mask] =\
            (pred[..., ~gtab.b0s_mask] +
             self.mean_signal[:, None]) * S0[:, None]

        return pred
