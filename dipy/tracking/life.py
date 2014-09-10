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
from dipy.tracking.utils import unique_rows, xform
import dipy.reconst.dti as dti

## XXX TODO : need to replace 'xform' with 'move_streamlines'

def apparent_diffusion_coef(bvecs, q):
    """
    This is the apparent diffusion

    $ADC = \vec{b} Q \vec{b}^T$
    """
    bvecs = np.matrix(bvecs)
    return np.diag(bvecs.T*q* bvecs)


class Fiber(ResetMixin):
    """
    This represents a single fiber, its node coordinates and statistics
    """

    def __init__(self, coords, affine=None, fiber_stats=None, node_stats=None):
        """
        Initialize a fiber

        Parameters
        ----------
        coords: np.array of shape 3 x n
            The x,y,z coordinates of the nodes comprising the fiber.

        affine: np.array of shape 4 x 4
            homogenous affine giving relationship between voxel-based
            coordinates and world-based (acpc) coordinates. Defaults to None,
            which implies the identity matrix.

        fiber_stats: dict containing statistics as scalars, corresponding to the
               entire fiber

        node_stats: dict containing statistics as np.array, corresponding
            to point-by-point values of the statistic.

        """
        coords = np.asarray(coords)
        if len(coords.shape)>2 or coords.shape[0]!=3:
            e_s = "coords input has shape ("
            e_s += ''.join(["%s, "%n for n in coords.shape])
            e_s += "); please reshape to be 3 by n"
            raise ValueError(e_s)

        self.coords = coords

        # Count the nodes
        if len(coords.shape)>1:
            self.n_nodes = coords.shape[-1]
        # This is the case in which there is only one coordinate/node:
        else:
            self.n_nodes = 1

        if affine is None:
            self.affine = None # This implies np.eye(4), see below in xform
        elif affine.shape != (4, 4):
            # Raise an erro if the affine doesn't make sense:
            e_s = "affine input has shape ("
            e_s += ''.join(["%s, "%n for n in affine.shape])
            e_s += "); please reshape to be 4 by 4"
            raise ValueError(e_s)
        else:
            self.affine = np.matrix(affine)

        if fiber_stats is not None:
            self.fiber_stats = fiber_stats
        else:
            # The default
            self.fiber_stats = {}

        if node_stats is not None:
            self.node_stats = node_stats
        else:
            # The default
            self.node_stats = {}


    def xform(self, affine=None, inplace=True):
        """
        Transform the fiber coordinates according to an affine transformation

        Parameters
        ----------
        affine: optional, 4 by 4 matrix
            Per default, the fiber's own affine will be used. If this input is
            provided, this affine will be used instead of the fiber's
            affine, and the new affine will be the inverse of this matrix.

        inplace: optional, bool
            Per default, the transformation occurs inplace, meaning that the
            Fiber is mutated inplace. However, if you don't want that to
            happen, setting this to False will cause this function to return
            another Fiber with transformed coordinates and the inverse of the
            original affine.

        Note
        ----
        Transforming inverts the affine, such that calling xform() twice gives
        you back what you had in the first place.

        """
        # If the affine optional kwarg was provided use that:
        if affine is None:
            if self.affine is None:
                if inplace:
                    return # Don't do anything and return
                else:
                    # Give me back an identical Fiber:
                    return Fiber(self.coords,
                                 None,
                                 self.fiber_stats,
                                 self.node_stats)

            # Use the affine provided on initialization:
            else:
                affine = self.affine

        # Do it:
        xyz_new = xform(self.coords, affine)

        # Just put the new coords instead of the old ones:
        if inplace:
            self.coords = xyz_new
            # And adjust the affine to be the inverse transform:
            self.affine = affine.getI()
        # Generate a new fiber and return it:
        else:
            return Fiber(self.coords,
                         affine.getI(),
                         self.fiber_stats,
                         self.node_stats)

    @auto_attr
    def unique_coords(self):
        """
        What are the unique spatial coordinates in the fiber
        """
        return unique_rows(self.coords.T).T


    @auto_attr
    def gradients(self):
        """
        The gradients along the fibers
        """
        return np.array(np.gradient(self.coords)[1])


    def tensors(self, axial_diffusivity, radial_diffusivity):
        """

        The tensors generated by this fiber.

        Parameters
        ----------
        fiber: A Fiber object.

        axial_diffusivity: float
            The estimated axial diffusivity of a single fiber tensor.

        radial_diffusivity: float
            The estimated radial diffusivity of a single fiber tensor.

        Returns
        -------
        An n_fiber by 3 by 3 array with the Q form for each node in the fiber.

        Note
        ----
        Estimates of the radial/axial diffusivities may rely on
        empirical measurements (for example, the AD in the Corpus Callosum), or
        may be based on a biophysical model of some kind.
        """

        d_matrix = np.matrix(np.diag([axial_diffusivity,
                                      radial_diffusivity,
                                      radial_diffusivity]))

        grad = self.gradients

        # Preallocate:
        tensors = np.empty((grad.shape[-1], 9)) #dtype='object')

        for grad_idx, this_grad in enumerate(grad.T):
            usv = la.svd(np.matrix(this_grad), overwrite_a=True)
            this_Q = (np.matrix(usv[2]) * d_matrix * np.matrix(usv[2]))
            tensors[grad_idx]= this_Q.ravel()

        return np.reshape(tensors, (-1, 3, 3))


    def predicted_signal(self,
                         gtab,
                         axial_diffusivity,
                         radial_diffusivity):
        """
        Compute the fiber contribution to the *relative signal* along its
        coords.

        Notes
        -----

        The calculation is based on a simplified Stejskal/Tanner equation:

        .. math::

           \frac{S/S_0} = exp^{-bval (\vec{b}*Q*\vec{b}^T)}

        Where $S0$ is the unweighted signal and $\vec{b} * Q * \vec{b}^t$ is
        the ADC for each tensor.

        To get the diffusion signal measured, you will have to multiply back by
        the $S_0$ in each voxel.

        Parameters
        ----------
        """
        # Gotta have those tensors:
        tens = self.tensors(axial_diffusivity,
                            radial_diffusivity)

        # Preallocate:
        sph = dps.Sphere(xyz=gtab.bvecs)
        ADC = dti.apparent_diffusion_coef(tens, sph)

        # Use the Stejskal-Tanner equation with the ADC as input, with S0 = 1:
        return np.exp(ADC)


class FiberGroup(ResetMixin):
    """
    This represents a group of fibers.
    """
    def __init__(self,
                 fibers,
                 name=None,
                 color=None,
                 thickness=None,
                 affine=None):
        """
        Initialize a group of fibers

        Parameters
        ----------
        fibers: list
            A set of Fiber objects, which will populate this FiberGroup.

        name: str
            Name of this fiber group, defaults to "FG-1"

        color: 3-long array or array like
            RGB for display of fibers from this FiberGroup. Defaults to
            [200, 200, 100]

        thickness: float
            The thickness when displaying this FiberGroup. Defaults to -0.5

        affine: 4 by 4 array or matrix
            Homogenous affine giving relationship between voxel-based
            coordinates and world-based (acpc) coordinates. Defaults to None,
            which implies the identity matrix.
        """
        # Descriptive variables:

        # Name
        if name is None:
            name = "FG-1"
        self.name = name

        if color is None:
            color = [200, 200, 100] # RGB
        self.color = np.asarray(color)

        if thickness is None:
            thickness = -0.5
        self.thickness = thickness

        self.fibers = fibers
        self.n_fibers = len(fibers)
        self.n_nodes = np.sum([f.n_nodes for f in self.fibers])
        # Gather all the unique fiber stat names:
        k_list = []
        # Get all the keys from each fiber:
        for fiber in self.fibers:
            k_list += fiber.fiber_stats.keys()

        # Get a set out (unique values):
        keys = set(k_list)
        # This will eventually hold all the fiber stats:
        self.fiber_stats = {}
        # Loop over unique stat names and...
        for k in keys:
            # ... put them in a list in each one ...
            self.fiber_stats[k] = []
            # ... from each fiber:
            for f_idx in range(self.n_fibers):
                this_fs = self.fibers[f_idx].fiber_stats
                # But only if that fiber has that stat:
                if k in this_fs.keys():
                    self.fiber_stats[k].append(this_fs[k])
                # Otherwise, put a nan there:
                else:
                    self.fiber_stats[k].append(np.nan)

        # If you want to give the FG an affine of its own to apply to the
        # fibers in it:
        if affine is not None:
            self.affine = np.matrix(affine)
        else:
            self.affine = None

    def xform(self, affine=None, inplace=True):
        """
        Transform each fiber in the fiber group according to an affine

        Precedence order : input > Fiber.affine > FiberGroup.affine

        Parameters
        ----------
        affine: 4 by 4 array/matrix
            An affine to apply instead of the affine provided by the Fibers
            themselves and instead of the affine provided by the FiberGroup

        inplace: Whether to change the FiberGroup/Fibers inplace.
        """
        if affine is None:
            affine = self.affine
            in_affine = False  # We need to be able to discriminate between
                               # affines provided by the class instance and
                               # affines provided as inputs
        else:
            in_affine = True

        if not inplace:
            fibs = np.copy(self.fibers) # Make a copy, to be able to do this not
                                        # inplace
        else:
            fibs = self.fibers # Otherwise, save memory by pointing to the same
                               # objects in memory

        for this_f in fibs:
            # This one takes the highest precedence:
            if in_affine:
                this_f.xform(np.matrix(affine))

            # Otherwise, the fiber affines take precedence:
            elif this_f.affine is not None:
                this_f.xform()
                affine = None # The resulting object should not have an
                              # affine.

            # And finally resort to the FG's affine:
            else:
                this_f.xform(self.affine)
                affine = self.affine # Invert the objects affine,
                                        # before assigning to the output

        if affine is not None:
            affine = np.matrix(affine).getI()

        if inplace:
            self.fibers = fibs
            self.affine = affine
            self.coords = self._get_coords()

        # If we asked to do things inplace, we are done. Otherwise, we return a
        # FiberGroup
        else:
            return FiberGroup(fibs,
                              name="FG-1",
                              color=[200, 200, 100],
                              thickness=-0.5,
                              affine=affine) # It's already been inverted above

    def __getitem__(self, i):
        """
        Overload __getitem__ to return the i'th fiber when indexing.
        """
        return self.fibers[i]

    def _get_coords(self):
        """
        Helper function which can be used to get the coordinates of the
        fibers. Useful for setting self.coords as an attr, but also allows to
        change that attr within self.xform
        """
        tmp = []
        for fiber in self.fibers:
            tmp.append(fiber.coords)

        # Concatenate 'em together:
        tmp = np.hstack(tmp)

        return tmp

    @auto_attr
    def coords(self):
        """
        Hold all the coords from all fibers.
        """
        return self._get_coords()

    @auto_attr
    def unique_coords(self):
        """
        The unique spatial coordinates of all the fibers in the FiberGroup.

        """
        return unique_rows(self.coords.T).T

    @auto_attr
    def idx(self):
        """
        Indices into the coordinates of the fiber-group
        """
        return self.coords.astype(int)


    @auto_attr
    def fg_idx_unique(self):
        """
        The *unique* voxel indices
        """
        return unique_rows(self.idx.T).T


    @auto_attr
    def voxel2fiber(self):
        """
        The first list in the tuple answers the question: Given a voxel (from
        the unique indices in this model), which fibers pass through it?

        The second answers the question: Given a voxel, for each fiber, which
        nodes are in that voxel?
        """
        # Make a voxels by fibers grid. If the fiber is in the voxel, the value
        # there will be 1, otherwise 0:
        v2f = np.zeros((len(self.fg_idx_unique.T), len(self.FG.fibers)))

        # This is a grid of size (fibers, maximal length of a fiber), so that
        # we can capture the voxel number in each fiber/node combination:
        v2fn = nans((len(self.FG.fibers),
                         np.max([f.coords.shape[-1] for f in self.FG])))

        # In each fiber:
        for f_idx, f in enumerate(self.FG.fibers):
            # In each voxel present in there:
            for vv in f.coords.astype(int).T:
                # What serial number is this voxel in the unique fiber indices:
                voxel_id = np.where((vv[0] == self.idx_unique[0]) *
                                    (vv[1] == self.idx_unique[1]) *
                                    (vv[2] == self.idx_unique[2]))[0]
                # Add that combination to the grid:
                v2f[voxel_id, f_idx] += 1
                # All the nodes going through this voxel get its number:
                v2fn[f_idx][np.where((f.coords.astype(int)[0]==vv[0]) *
                                     (f.coords.astype(int)[1]==vv[1]) *
                                     (f.coords.astype(int)[2]==vv[2]))]=voxel_id

            if self.verbose:
                prog_bar.animate(f_idx, f_name=f_name)

        return v2f,v2fn


class FiberModel(ReconstModel):
    """

    A class for representing and solving predictive models based on
    tractography solutions.

    """
    def __init__(self, gtab, axial_diffusivity=1.5, radial_diffusivity=0.5):
        """
        Parameters
        ----------

        FG: a osmosis.fibers.FiberGroup object, or the name of a pdb file
            containing the fibers to be read in using ozf.fg_from_pdb

        axial_diffusivity: The axial diffusivity of a single fiber population.

        radial_diffusivity: The radial diffusivity of a single fiber population.

        """
        # Initialize the super-class:
        ReconstModel.__init__(self, gtab)
        # Set axial and radial diffusivity for the response function:
        self.axial_diffusivity = axial_diffusivity
        self.radial_diffusivity = radial_diffusivity


    def fiber_signal(self, data, fiber_group):
        sig = []
        for f_idx, f in enumerate(fiber_group):
            sig.append(f.predicted_signal(self.gtab.bvecs[:, self.b_idx],
                                          self.gtab.bvals[self.b_idx],
                                          self.axial_diffusivity,
                                          self.radial_diffusivity))
        return sig


    def matrix(self, data, fiber_group):
        """
        The matrix of fiber-contributions to the DWI signal.
        """
        # Assign some local variables, for shorthand:
        vox_coords = fiber_group.idx_unique.T
        n_vox = fiber_group.idx_unique.shape[-1]
        n_bvecs = self.gtab
        v2f,v2fn = fiber_group.voxel2fiber

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

        # In each voxel:
        for v_idx, vox in enumerate(vox_coords):
            # For each fiber:
            for f_idx in np.where(v2f[v_idx])[0]:
                # Sum the signal from each node of the fiber in that voxel:
                pred_sig = np.zeros(n_bvecs)
                for n_idx in np.where(v2fn[f_idx]==v_idx)[0]:
                    relative_signal = self.fiber_signal[f_idx][n_idx]
                    if self.mode == 'relative_signal':
                        # Predict the signal and demean it, so that the isotropic
                        # part can carry that:
                        pred_sig += (relative_signal -
                            np.mean(self.relative_signal[vox[0],vox[1],vox[2]]))
                    elif self.mode == 'signal_attenuation':
                        pred_sig += ((1 - relative_signal) -
                        np.mean(1 - self.relative_signal[vox[0],vox[1],vox[2]]))

            # For each fiber-voxel combination, we now store the row/column
            # indices and the signal in the pre-allocated linear arrays
            f_matrix_row[keep_ct1:keep_ct1+n_bvecs] =\
                np.arange(n_bvecs) + v_idx * n_bvecs
            f_matrix_col[keep_ct1:keep_ct1+n_bvecs] = np.ones(n_bvecs) * f_idx
            f_matrix_sig[keep_ct1:keep_ct1+n_bvecs] = pred_sig
            keep_ct1 += n_bvecs

            # Put in the isotropic part in the other matrix:
            i_matrix_row[keep_ct2:keep_ct2+n_bvecs]=\
                np.arange(v_idx*n_bvecs, (v_idx + 1)*n_bvecs)
            i_matrix_col[keep_ct2:keep_ct2+n_bvecs]= v_idx * np.ones(n_bvecs)
            i_matrix_sig[keep_ct2:keep_ct2+n_bvecs] = 1
            keep_ct2 += n_bvecs
            if self.verbose:
                prog_bar.animate(v_idx, f_name=f_name)

        # Allocate the sparse matrices, using the more memory-efficient 'csr'
        # format:
        fiber_matrix = sparse.coo_matrix((f_matrix_sig,
                                       [f_matrix_row, f_matrix_col])).tocsr()
        iso_matrix = sparse.coo_matrix((i_matrix_sig,
                                       [i_matrix_row, i_matrix_col])).tocsr()

        return (fiber_matrix, iso_matrix)


    def voxel_signal(self, data, fiber_group):
        """
        The signal in the voxels corresponding to where the fibers pass through.
        """
        b0 = np.mean(data[... self.gtab.b0s_mask], -1)
        if self.mode == 'relative_signal':
            return self.relative_signal[self.fg_idx_unique[0],
                                        self.fg_idx_unique[1],
                                        self.fg_idx_unique[2]]


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
