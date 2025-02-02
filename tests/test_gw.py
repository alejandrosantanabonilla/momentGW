import unittest
import numpy as np
from pyscf import gto, dft, gw, tdscf, lib
from pyscf.data.nist import HARTREE2EV
from momentGW import GW


class KnownValues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mol = gto.Mole()
        mol.atom = "O 0 0 0; H 0 -0.7571 0.5861; H 0 0.7571 0.5861"
        mol.atom = "O 0 0 0; O 0 0 1"
        mol.basis = "cc-pvdz"
        mol.verbose = 0
        mol.build()

        mf = dft.RKS(mol)
        mf.xc = "hf"
        mf.conv_tol = 1e-11
        mf.kernel()

        gw_exact = gw.GW(mf, freq_int="exact")
        gw_exact.kernel()

        mf = mf.density_fit(auxbasis="cc-pv5z-ri")
        mf.with_df.build()

        cls.mol, cls.mf, cls.gw_exact = mol, mf, gw_exact

    @classmethod
    def tearDownClass(cls):
        del cls.mol, cls.mf, cls.gw_exact

    def test_vs_pyscf_vhf_df(self):
        gw = GW(self.mf)
        gw.diagonal_se = True
        gw.vhf_df = True
        conv, gf, se = gw.kernel(nmom_max=7)
        gf.remove_uncoupled(tol=1e-8)
        self.assertAlmostEqual(
                gf.get_occupied().energy.max(),
                self.gw_exact.mo_energy[self.gw_exact.mo_occ > 0].max(),
                2,
        )
        self.assertAlmostEqual(
                gf.get_virtual().energy.min(),
                self.gw_exact.mo_energy[self.gw_exact.mo_occ == 0].min(),
                2,
        )

    def test_vs_pyscf_no_vhf_df(self):
        gw = GW(self.mf)
        gw.diagonal_se = True
        gw.vhf_df = False
        conv, gf, se = gw.kernel(nmom_max=7)
        gf.remove_uncoupled(tol=1e-8)
        self.assertAlmostEqual(
                gf.get_occupied().energy.max(),
                self.gw_exact.mo_energy[self.gw_exact.mo_occ > 0].max(),
                2,
        )
        self.assertAlmostEqual(
                gf.get_virtual().energy.min(),
                self.gw_exact.mo_energy[self.gw_exact.mo_occ == 0].min(),
                2,
        )

    def test_nelec(self):
        gw = GW(self.mf)
        gw.diagonal_se = True
        gw.vhf_df = False
        conv, gf, se = gw.kernel(nmom_max=1)
        self.assertAlmostEqual(
                gf.make_rdm1().trace(),
                self.mol.nelectron,
                1,
        )
        gw.optimise_chempot = True
        gw.vhf_df = False
        conv, gf, se = gw.kernel(nmom_max=1)
        self.assertAlmostEqual(
                gf.make_rdm1().trace(),
                self.mol.nelectron,
                8,
        )

    def test_moments(self):
        gw = GW(self.mf)
        gw.diagonal_se = True
        gw.vhf_df = False
        th1, tp1 = gw.build_se_moments(5, gw.ao2mo(self.mf.mo_coeff))
        conv, gf, se = gw.kernel(nmom_max=5)
        th2 = se.get_occupied().moment(range(5))
        tp2 = se.get_virtual().moment(range(5))

        for a, b in zip(th1, th2):
            dif = np.max(np.abs(a - b)) / np.max(np.abs(a))
            self.assertAlmostEqual(dif, 0, 8)
        for a, b in zip(tp1, tp2):
            dif = np.max(np.abs(a - b)) / np.max(np.abs(a))
            self.assertAlmostEqual(dif, 0, 8)

    def test_moments_vs_tdscf(self):
        gw = GW(self.mf)
        gw.diagonal_se = True
        nocc, nvir = gw.nocc, gw.nmo-gw.nocc
        th1, tp1 = gw.build_se_moments(5, gw.ao2mo(self.mf.mo_coeff))

        td = tdscf.dRPA(self.mf)
        td.nstates = nocc*nvir
        td.kernel()
        z = np.sum(np.array(td.xy)*2, axis=1).reshape(len(td.e), nocc, nvir)
        Lpq = gw.ao2mo(self.mf.mo_coeff)

        m = lib.einsum("Qia,via,Qpj->vpj", Lpq[:, :nocc, nocc:], z, Lpq[:, :, :nocc])
        e = lib.direct_sum("j-v->jv", self.mf.mo_energy[:nocc], td.e)
        th2 = []
        for n in range(6):
            t = lib.einsum("vpj,jv,vqj->pq", m, np.power(e, n), m)
            if gw.diagonal_se:
                t = np.diag(np.diag(t))
            th2.append(t)

        m = lib.einsum("Qia,via,Qqb->vqb", Lpq[:, :nocc, nocc:], z, Lpq[:, :, nocc:])
        e = lib.direct_sum("b+v->bv", self.mf.mo_energy[nocc:], td.e)
        tp2 = []
        for n in range(6):
            t = lib.einsum("vpj,jv,vqj->pq", m, np.power(e, n), m)
            if gw.diagonal_se:
                t = np.diag(np.diag(t))
            tp2.append(t)

        for a, b in zip(th1, th2):
            dif = np.max(np.abs(a - b)) / np.max(np.abs(a))
            self.assertAlmostEqual(dif, 0, 8)
        for a, b in zip(tp1, tp2):
            dif = np.max(np.abs(a - b)) / np.max(np.abs(a))
            self.assertAlmostEqual(dif, 0, 8)



if __name__ == "__main__":
    print("Running tests for GW")
    unittest.main()
