"""
This is an implementation of the Linear Fascicle Evaluation (LiFE) algorithm
described in:

Pestilli, F., Yeatman, J, Rokem, A. Kay, K. and Wandell B.A. (2014). Validation
and statistical inference in living connectomes. Nature Methods

"""
import numpy as np
import scipy.sparse as sparse
import scipy.linalg as la

from dipy.reconst.base import ReconstModel, ReconstFit
from dipy.core.onetime import ResetMixin
from dipy.core.onetime import auto_attr
import dipy.core.sphere as dps
from dipy.tracking.utils import unique_rows, xform, move_streamlines
import dipy.reconst.dti as dti
import dipy.sims.voxel as sims


def sl_gradients(sl):
    """
    Calculate the gradients of the stream-line along the spatial dimension
    """
    return np.array(np.gradient(sl)[0])


def grad_tensor(grad, evals):
    """
    Calculate the 3 by 3 tensor for a given spatial gradient, given a canonical
    tensor shape (also as a 3 by 3), pointing at [1,0,0]

    Parameters
    ----------
    grad : 1d array of shape (3,)
        The spatial gradient (e.g between two nodes of a streamline).

    evals: 1d array of shape (3,)
        The eigenvalues of a canonical tensor to be used as a response function.

    """
    # This is the rotation matrix from [1, 0, 0] to this gradient of the sl:
    R = la.svd(np.matrix(grad), overwrite_a=True)[2]
    # This is the quadratic form of the tensor after rotation:
    Q = np.dot(np.dot(R, np.diag(evals)), R.T)
    return Q

def sl_tensors(sl, evals=[0.0015, 0.0005, 0.0005]):
    """

    The tensors generated by this fiber.

    Parameters
    ----------
    sl :

    evals : iterable with three entries
        The estimated eigenvalues of a single fiber tensor.
        (default: [0.0015, 0.0005, 0.0005]).

    Returns
    -------
    An n_nodes by 3 by 3 array with the Q form for each node in the fiber.

    Note
    ----
    Estimates of the radial/axial diffusivities may rely on
    empirical measurements (for example, the AD in the Corpus Callosum), or
    may be based on a biophysical model of some kind.
    """

    grad = sl_gradients(sl)

    # Preallocate:
    tensors = np.empty((grad.shape[0], 3, 3))

    for grad_idx, this_grad in enumerate(grad):
        tensors[grad_idx] = grad_tensor(this_grad, evals)
    return tensors

def sl_signal(sl, gtab, evals):
    """
    The signal from a single streamline estimate along each of its nodes.

    Parameters
    ----------
    sl : a single streamline

    gtab : a sphere on which to estimate the signal (presumably made from
    your bvecs)

    XXX We might be able to make the whole thing go faster by feeding it a
    sphere and bvals, instead of generating the sphere every time anew
    """

    sph = dps.Sphere(xyz=gtab.bvecs)
    # Gotta have those tensors:
    tensors = sl_tensors(sl, evals)
    sig = np.empty((len(sl), np.sum(~gtab.b0s_mask)))
    # Extract them once:
    bvecs = gtab.bvecs[~gtab.b0s_mask]
    bvals = gtab.bvals[~gtab.b0s_mask]
    for ii, tensor in enumerate(tensors):
        ADC = np.diag(np.dot(np.dot(bvecs, tensor), bvecs.T))
        # Use the Stejskal-Tanner equation with the ADC as input, and S0 = 1:
        sig[ii] = np.exp(-bvals * ADC)
    return sig


def transform_sl(sl, affine=None):
    """
    Helper function that moves and generates the streamline. Thin wrapper
    around move_streamlines

    Parameters
    ----------
    sl : list
        A list of streamline coordinates

    affine : 4 by 4 array
        Affine mapping from fibers to data
    """
    if affine is None:
        affine = np.eye(4)
    # Generate these immediately:
    return [s for s in move_streamlines(sl, affine)]



def voxel2fiber(sl, transformed=False, affine=None, unique_idx=None):
    """
    Maps voxels to stream-lines and stream-lines to voxels, for setting up
    the LiFE equations matrix

    Parameters
    ----------
    sl : list
        A collection of streamlines, each n by 3, with n being the number of
        nodes in the fiber.

    affine : 4 by 4 array (optional)
       Defines the spatial transformation from sl to data. Default: np.eye(4)

    transformed : bool (optional)
        Whether the streamlines have been already transformed (in which case
        they don't need to be transformed in here).

    unique_idx : array (optional).
       The unique indices in the streamlines

    Returns
    -------
    v2f, v2fn : tuple of arrays

    The first array in the tuple answers the question: Given a voxel (from
    the unique indices in this model), which fibers pass through it? Shape:
    (n_voxels, n_fibers).

    The second answers the question: Given a voxel, for each fiber, which
    nodes are in that voxel? Shape: (n_voxels, max(n_nodes per fiber)).

    """
    if transformed:
        transformed_sl = sl
    else:
        transformed_sl = transform_sl(sl, affine=affine)

    if unique_idx is None:
        all_coords = np.concatenate(transformed_sl)
        unique_idx = unique_rows(all_coords.astype(int))
    else:
        unique_idx = unique_idx

    # Given a voxel (from the unique coords, is the fiber in here?)
    v2f = np.zeros((len(unique_idx), len(sl)), int)

    # This is a grid of size (fibers, maximal length of a fiber), so that
    # we can capture the voxel number in each fiber/node combination:
    v2fn = np.ones((len(sl), np.max([len(s) for s in sl])), int) * np.nan

    # In each fiber:
    for s_idx, s in enumerate(transformed_sl):
        sl_as_idx = np.array(s).astype(int)
        # In each voxel present in there:
        for vv in sl_as_idx:
            # What serial number is this voxel in the unique streamline indices:
            voxel_id = int(np.where((vv[0] == unique_idx[:, 0]) *
                                    (vv[1] == unique_idx[:, 1]) *
                                    (vv[2] == unique_idx[:, 2]))[0])

            # Add that combination to the grid:
            v2f[voxel_id, s_idx] += 1

            # All the nodes going through this voxel get its number:
            v2fn[s_idx][np.where((sl_as_idx[:, 0] == vv[0]) *
                                 (sl_as_idx[:, 1] == vv[1]) *
                                 (sl_as_idx[:, 2] == vv[2]))] = voxel_id

    return v2f ,v2fn


class FiberModel(ReconstModel):
    """
    A class for representing and solving predictive models based on
    tractography solutions.

    Notes
    -----

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


    def fit(self, data, sl, affine=None, evals=[0.0015, 0.0005, 0.0005]):
        """
        Fit the LiFE FiberModel for data and a set of streamlines associated
        with this data

        Parameters
        ----------
        data : 4D array
           Diffusion-weighted data

        sl : list
           A bunch of streamlines

        """
        fiber_matrix, iso_matrix, vox_coords = \
            self.model_setup(sl, affine, evals=evals)

        # Fitting is done on the S0-normalized-and-demeaned diffusion-weighted
        # signal:
        relative_signal = (data[~self.gtab.b0s_mask]/
                           np.mean(data[self.gtab.b0s_mask])

        to_fit = relative_signal - np.mean(relative_signal, -1)


        return FiberFit(self, )

    def model_setup(self, sl, affine, evals=[0.0015, 0.0005, 0.0005]):
        """
        The matrix of fiber-contributions to the DWI signal.

        """

        # XXX Need to use `yield` to generate the vox_coords, without holding
        # all of the fibers in memory simultaneously - compare the current set
        # of vox coords to the next fiber in each iteration.
        sl = transform_sl(sl, affine)
        # Assign some local variables, for shorthand:
        all_coords = np.concatenate(sl)
        vox_coords = unique_rows(all_coords.astype(int))
        n_vox = vox_coords.shape[0]
        # We only consider the diffusion-weighted signals:
        n_bvecs = self.gtab.bvals[~self.gtab.b0s_mask].shape[0]

        v2f, v2fn = voxel2fiber(sl, transformed=True, affine=affine,
                                unique_idx=vox_coords)

        # How many fibers in each voxel (this will determine how many
        # components are in the fiber part of the matrix):
        n_unique_f = np.sum(v2f)

        # Preallocate these, which will be used to generate the two sparse
        # matrices:

        # This one will hold the fiber-predicted signal
        f_matrix_sig = np.zeros(n_unique_f * n_bvecs)
        f_matrix_row = np.zeros(n_unique_f * n_bvecs)
        f_matrix_col = np.zeros(n_unique_f * n_bvecs)

        # And this will hold weights to soak up the isotropic component in each
        # voxel:
        i_matrix_sig = np.zeros(n_vox * n_bvecs)
        i_matrix_row = np.zeros(n_vox * n_bvecs)
        i_matrix_col = np.zeros(n_vox * n_bvecs)

        keep_ct1 = 0
        keep_ct2 = 0

        fiber_signal = [sl_signal(s, self.gtab, evals) for s in sl]

        # In each voxel:
        for v_idx, vox in enumerate(vox_coords):
            # For each fiber:
            for f_idx in np.where(v2f[v_idx])[0]:
                # Sum the signal from each node of the fiber in that voxel:
                pred_sig = np.zeros(n_bvecs)
                for n_idx in np.where(v2fn[f_idx]==v_idx)[0]:
                    this_signal = fiber_signal[f_idx][n_idx]
                    # Predict the signal and demean it, so that the isotropic
                    # part can carry that:
                    pred_sig += (this_signal - np.mean(this_signal))

            # For each fiber-voxel combination, we now store the row/column
            # indices and the signal in the pre-allocated linear arrays
            f_matrix_row[keep_ct1:keep_ct1+n_bvecs] =\
                np.arange(n_bvecs) + v_idx * n_bvecs
            f_matrix_col[keep_ct1:keep_ct1+n_bvecs] = np.ones(n_bvecs) * f_idx
            f_matrix_sig[keep_ct1:keep_ct1+n_bvecs] = pred_sig
            keep_ct1 += n_bvecs

            # Put in the isotropic part in the other matrix:
            i_matrix_row[keep_ct2:keep_ct2 + n_bvecs] =\
                np.arange(v_idx * n_bvecs, (v_idx + 1) * n_bvecs)
            i_matrix_col[keep_ct2:keep_ct2 + n_bvecs] = v_idx * np.ones(n_bvecs)
            i_matrix_sig[keep_ct2:keep_ct2 + n_bvecs] = 1
            keep_ct2 += n_bvecs

        # Allocate the sparse matrices, using the more memory-efficient 'csr'
        # format (converted from the coo format, which we rely on for the
        # initial allocation):
        fiber_matrix = sparse.coo_matrix((f_matrix_sig,
                                       [f_matrix_row, f_matrix_col])).tocsr()
        iso_matrix = sparse.coo_matrix((i_matrix_sig,
                                       [i_matrix_row, i_matrix_col])).tocsr()

        return (fiber_matrix, iso_matrix, vox_coords)




    @auto_attr
    def voxel_signal_demeaned(self):
        """
        The signal in the voxels corresponding to where the fibers pass
        through, with mean removed
        """
        # Get the average, broadcast it back to the original shape and demean,
        # finally ravel again:
        return(self.voxel_signal.ravel() -
               (np.mean(self.voxel_signal,-1)[np.newaxis,...] +
        np.zeros((len(self.b_idx),self.voxel_signal.shape[0]))).T.ravel())


    @auto_attr
    def iso_weights(self):
        """
        Get the isotropic weights
        """
        # XXX Need to replace SGD with another solver:
        iso_w = sgd.stochastic_gradient_descent(self.voxel_signal.ravel(),
                                                self.matrix[1],
                                                verbose=self.verbose)
        return iso_w

    @auto_attr
    def fiber_weights(self):
        """
        Get the weights for the fiber part of the matrix
        """
        # XXX Need to replace SGD with another solver:
        fiber_w = sgd.stochastic_gradient_descent(self.voxel_signal_demeaned,
                                                  self.matrix[0],
                                                  verbose=self.verbose)

        return fiber_w


    @auto_attr
    def _fiber_predict(self):
        """
        This is the fit for the non-isotropic part of the signal:
        """
        # return self._Lasso.predict(self.matrix[0])
        return sgd.spdot(self.matrix[0], self.fiber_weights)


    @auto_attr
    def _iso_predict(self):
        # We want this to have the size of the original signal which is
        # (n_bvecs * n_vox), so we broadcast across directions in each voxel:
        return (self.iso_weights[np.newaxis,...] +
                np.zeros((len(self.b_idx), self.iso_weights.shape[0]))).T.ravel()




def FiberFit(ReconstFit):
    """
    A fit of the LiFE model to diffusion data
    """

    def __init__(self, fiber_model, params)

    @auto_attr
    def predict(self):
        """
        The predicted signal from the FiberModel
        """
        # We generate the prediction and in each voxel, we add the
        # offset, according to the isotropic part of the signal, which was
        # removed prior to fitting:
        # XXX Still need to multiply by b0 in the end to get it to the signal
        # in scanner units
        return np.array(self._fiber_fit + self._iso_fit).squeeze()
