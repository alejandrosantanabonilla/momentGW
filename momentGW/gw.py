"""
Spin-restricted G0W0 via self-energy moment constraints for
molecular systems.
"""

import numpy as np

from pyscf import lib, scf
from pyscf.lib import logger
from pyscf.ao2mo import _ao2mo
from pyscf.agf2 import chempot, GreensFunction, SelfEnergy

from dyson import MBLSE, MixedMBL, NullLogger

from momentGW import rpa
from momentGW.base import BaseGW


def kernel(
        gw,
        nmom_max,
        mo_energy,
        mo_coeff,
        moments=None,
        Lpq=None,
        vhf_df=None,
        npoints=48,
        verbose=logger.NOTE,
):
    """Moment-constrained G0W0.

    Parameters
    ----------
    gw : BaseGW
        GW object.
    nmom_max : int
        Maximum moment number to calculate.
    mo_energy : numpy.ndarray
        Molecular orbital energies.
    mo_coeff : numpy.ndarray
        Molecular orbital coefficients.
    moments : tuple of numpy.ndarray, optional
        Tuple of (hole, particle) moments, if passed then they will
        be used instead of calculating them. Default value is None.
    Lpq : np.ndarray, optional
        Density-fitted ERI tensor. If None, generate from `gw.ao2mo`.
        Default value is None.
    vhf_df : bool, optional
        If True, calculate the static self-energy directly from `Lpq`.
        Default value is False.

    Returns
    -------
    conv : bool
        Convergence flag. Always True for AGW, returned for
        compatibility with other GW methods.
    gf : pyscf.agf2.GreensFunction
        Green's function object
    se : pyscf.agf2.SelfEnergy
        Self-energy object
    """

    if Lpq is None:
        Lpq = gw.ao2mo(mo_coeff)

    se_static = gw.build_se_static(
            Lpq=Lpq,
            mo_energy=mo_energy,
            mo_coeff=mo_coeff,
            vhf_df=vhf_df,
    )

    if moments is None:
        th, tp = gw.build_se_moments(
                nmom_max,
                Lpq=Lpq,
                mo_energy=mo_energy,
                mo_coeff=mo_coeff,
                npoints=npoints,
        )
    else:
        th, tp = moments

    gf, se = gw.solve_dyson(th, tp, se_static)
    conv = True

    logger.debug(gw, "Error in moments: occ=%.6g  vir=%.6g", *gw.moment_error(th, tp, se))

    return conv, gf, se


class GW(BaseGW):
    """Spin-restricted G0W0 via self-energy moment constraints for
    molecular systems.
    """

    def build_se_static(self, Lpq=None, vhf_df=False, mo_coeff=None, mo_energy=None):
        """Build the static part of the self-energy, including the
        Fock matrix.

        Parameters
        ----------
        Lpq : np.ndarray, optional
            Density-fitted ERI tensor. If None, generate from `gw.ao2mo`.
            Default value is None.
        vhf_df : bool, optional
            If True, calculate the static self-energy directly from `Lpq`.
            Default value is False.
        mo_energy : numpy.ndarray, optional
            Molecular orbital energies.  Default value is that of
            `self._scf.mo_energy`.
        mo_coeff : numpy.ndarray
            Molecular orbital coefficients.  Default value is that of
            `self._scf.mo_coeff`.

        Returns
        -------
        se_static : numpy.ndarray
            Static part of the self-energy. If `self.diagonal_se`,
            non-diagonal elements are set to zero.
        """

        if mo_coeff is None:
            mo_coeff = self._scf.mo_coeff
        if mo_energy is None:
            mo_energy = self._scf.mo_energy
        if Lpq is None and vhf_df:
            Lpq = self.ao2mo(mo_coeff)

        v_mf = self._scf.get_veff() - self._scf.get_j()
        v_mf = lib.einsum("pq,pi,qj->ij", v_mf, mo_coeff, mo_coeff)

        # v_hf from DFT/HF density
        if vhf_df:
            sc = np.dot(self._scf.get_ovlp(), mo_coeff)
            dm = lib.einsum("pq,pi,qj->ij", self._scf.make_rdm1(mo_coeff=mo_coeff), sc, sc)
            tmp = lib.einsum("Qik,kl->Qil", Lpq, dm)
            vk = -lib.einsum("Qil,Qlj->ij", tmp, Lpq) * 0.5
        else:
            dm = self._scf.make_rdm1(mo_coeff=mo_coeff)
            vk = scf.hf.SCF.get_veff(self._scf, self.mol, dm) - scf.hf.SCF.get_j(
                self._scf, self.mol, dm
            )
            vk = lib.einsum("pq,pi,qj->ij", vk, mo_coeff, mo_coeff)

        se_static = vk - v_mf

        if self.diagonal_se:
            se_static = np.diag(np.diag(se_static))

        se_static += np.diag(mo_energy)

        return se_static

    def ao2mo(self, mo_coeff):
        """Get the density-fitted integrals.
        """

        mo = np.asarray(mo_coeff, order="F")
        nmo = mo.shape[-1]
        ijslice = (0, nmo, 0, nmo)
        
        Lpq = _ao2mo.nr_e2(self.with_df._cderi, mo, ijslice, aosym="s2", out=None)

        return Lpq.reshape(-1, nmo, nmo)

    def build_se_moments(self, nmom_max, Lpq=None, mo_energy=None, mo_coeff=None, npoints=48):
        """Build the moments of the self-energy.

        Parameters
        ----------
        nmom_max : int
            Maximum moment number to calculate.
        Lpq : np.ndarray, optional
            Density-fitted ERI tensor. If None, generate from `gw.ao2mo`.
            Default value is None.
        vhf_df : bool, optional
            If True, calculate the static self-energy directly from `Lpq`.
            Default value is False.
        mo_energy : numpy.ndarray, optional
            Molecular orbital energies.  Default value is that of
            `self._scf.mo_energy`.
        mo_coeff : numpy.ndarray
            Molecular orbital coefficients.  Default value is that of
            `self._scf.mo_coeff`.
        npoints : int, optional
            Number of quadrature points to use. Default value is 48.

        Returns
        -------
        se_moments_hole : numpy.ndarray
            Moments of the hole self-energy. If `self.diagonal_se`,
            non-diagonal elements are set to zero.
        se_moments_part : numpy.ndarray
            Moments of the particle self-energy. If `self.diagonal_se`,
            non-diagonal elements are set to zero.
        """

        # Check if we can use the optimised routine
        if self.polarizability == "drpa" and Lpq is not None:
            return rpa.build_se_moments_drpa_opt(
                    self,
                    nmom_max,
                    Lpq,
                    mo_energy=mo_energy,
                    npoints=npoints,
            )
        else:
            raise NotImplementedError

    def solve_dyson(self, se_moments_hole, se_moments_part, se_static):
        """Solve the Dyson equation due to a self-energy resulting
        from a list of hole and particle moments, along with a static
        contribution.

        Also finds a chemical potential best satisfying the physical
        number of electrons. If `self.optimise_chempot`, this will
        shift the self-energy poles relative to the Green's function,
        which is a partial self-consistency that better conserves the
        particle number.

        Parameters
        ----------
        se_moments_hole : numpy.ndarray
            Moments of the hole self-energy.
        se_moments_part : numpy.ndarray
            Moments of the particle self-energy.
        se_static : numpy.ndarray
            Static part of the self-energy.

        Returns
        -------
        gf : pyscf.agf2.GreensFunction
            Green's function.
        se : pyscf.agf2.SelfEnergy
            Self-energy.
        """

        nlog = NullLogger()

        solver_occ = MBLSE(se_static, np.array(se_moments_hole), log=nlog)
        solver_occ.kernel()

        solver_vir = MBLSE(se_static, np.array(se_moments_part), log=nlog)
        solver_vir.kernel()

        solver = MixedMBL(solver_occ, solver_vir)
        e_aux, v_aux = solver.get_auxiliaries()
        se = SelfEnergy(e_aux, v_aux)

        if self.optimise_chempot:
            se, opt = chempot.minimize_chempot(se, se_static, gw.nocc*2)

        gf = se.get_greens_function(se_static)

        try:
            cpt, error = chempot.binsearch_chempot(
                    (gf.energy, gf.coupling), gf.nphys, self.nocc*2,
            )
        except:
            cpt = gf.chempot
            error = np.trace(gf.make_rdm1()) - gw.nocc*2

        se.chempot = cpt
        gf.chempot = cpt
        logger.info(self, "Error in number of electrons: %.5g", error)

        return gf, se

    def moment_error(self, se_moments_hole, se_moments_part, se):
        """Return the error in the moments.
        """
        
        eh = self._moment_error(
                se_moments_hole,
                se.get_occupied().moment(range(len(se_moments_hole))),
        )
        ep = self._moment_error(
                se_moments_part,
                se.get_virtual().moment(range(len(se_moments_part))),
        )

        return eh, ep

    def kernel(
            self,
            nmom_max,
            mo_energy=None,
            mo_coeff=None,
            moments=None,
            Lpq=None,
            vhf_df=None,
            npoints=48,
            verbose=logger.NOTE,
    ):
        if mo_coeff is None:
            mo_coeff = self._scf.mo_coeff
        if mo_energy is None:
            mo_energy = self._scf.mo_energy

        cput0 = (logger.process_clock(), logger.perf_counter())
        self.dump_flags()
        logger.info(self, "nmom_max = %d", nmom_max)

        self.converged, self.gf, self.se = kernel(
                self,
                nmom_max,
                mo_energy,
                mo_coeff,
                Lpq=Lpq,
                vhf_df=vhf_df,
                npoints=npoints,
                verbose=self.verbose,
        )

        gf_occ = self.gf.get_occupied()
        gf_occ.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_occ.naux)):
            en = gf_occ.energy[-(n + 1)]
            vn = gf_occ.coupling[:, -(n + 1)]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(
                self, "IP energy level %d E = %.16g  QP weight = %0.6g", n, en, qpwt
            )

        gf_vir = self.gf.get_virtual()
        gf_vir.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_vir.naux)):
            en = gf_vir.energy[n]
            vn = gf_vir.coupling[:, n]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(
                self, "EA energy level %d E = %.16g  QP weight = %0.6g", n, en, qpwt
            )

        logger.timer(self, "GW", *cput0)

        return self.converged, self.gf, self.se

G0W0 = GW
