#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# For tlm_adjoint copyright information see ACKNOWLEDGEMENTS in the tlm_adjoint
# root directory

# This file is part of tlm_adjoint.
#
# tlm_adjoint is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# tlm_adjoint is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with tlm_adjoint.  If not, see <https://www.gnu.org/licenses/>.

from fenics import *
from tlm_adjoint.fenics import *
from tlm_adjoint.fenics import manager as _manager

# import h5py
import mpi4py.MPI as MPI
import numpy as np
# import petsc4py.PETSc as PETSc

stop_manager()
# PETSc.Options().setValue("citations", "petsc.bib")
np.random.seed(87838678 + MPI.COMM_WORLD.rank)

mesh = UnitSquareMesh(50, 50)
space = FunctionSpace(mesh, "Lagrange", 1)
test, trial = TestFunction(space), TrialFunction(space)
bc = HomogeneousDirichletBC(space, "on_boundary")

dt = Constant(0.01, static=True)
N = 10
kappa = Function(space, name="kappa", static=True)
function_assign(kappa, 1.0)
Psi_0 = Function(space, name="Psi_0", static=True)
Psi_0.interpolate(Expression("exp(x[0]) * sin(pi * x[0])"
                             + " * sin(10.0 * pi * x[0])"
                             + " * sin(2.0 * pi * x[1])",
                             element=space.ufl_element()))

zeta_1 = Function(space, name="zeta_1", static=True)
zeta_2 = Function(space, name="zeta_2", static=True)
zeta_3 = ZeroFunction(space, name="zeta_3")
function_set_values(zeta_1,
                    2.0 * np.random.random(function_local_size(zeta_1)) - 1.0)
function_set_values(zeta_2,
                    2.0 * np.random.random(function_local_size(zeta_2)) - 1.0)
# File("zeta_1.pvd", "compressed") << zeta_1
# File("zeta_2.pvd", "compressed") << zeta_2


def forward(kappa, manager=None, output_filename=None):
    clear_caches()

    Psi_n = Function(space, name="Psi_n")
    Psi_np1 = Function(space, name="Psi_np1")

    eq = EquationSolver(inner(trial / dt, test) * dx
                        + inner(kappa * grad(trial), grad(test)) * dx
                        == inner(Psi_n / dt, test) * dx, Psi_np1,
                        bc, solver_parameters={"linear_solver": "cg",
                                               "preconditioner": "sor",
                                               "krylov_solver": {"absolute_tolerance": 1.0e-16,  # noqa: E501
                                                                 "relative_tolerance": 1.0e-14}})  # noqa: E501
    cycle = AssignmentSolver(Psi_np1, Psi_n)

    if output_filename is not None:
        f = File(output_filename, "compressed")

    AssignmentSolver(Psi_0, Psi_n).solve(manager=manager)
    if output_filename is not None:
        f << (Psi_n, 0.0)
    for n in range(N):
        eq.solve(manager=manager)
        if n < N - 1:
            cycle.solve(manager=manager)
            (_manager() if manager is None else manager).new_block()
        else:
            Psi_n = Psi_np1
            Psi_n.rename("Psi_n", "a Function")
            del Psi_np1
        if output_filename is not None:
            f << (Psi_n, (n + 1) * float(dt))

    J = Functional(name="J")
    J.assign(dot(Psi_n, Psi_n) * dx, manager=manager)

    return J


configure_tlm((kappa, zeta_1), ((kappa, zeta_1), (zeta_2, zeta_3)))
start_manager()
# J = forward(kappa, output_filename="forward.pvd")
J = forward(kappa)
dJ_tlm_1 = J.tlm_functional((kappa, zeta_1))
dJ_tlm_2 = J.tlm_functional(((kappa, zeta_1), (zeta_2, zeta_3)))
ddJ_tlm = dJ_tlm_1.tlm_functional(((kappa, zeta_1), (zeta_2, zeta_3)))
stop_manager()

dJ_adj, ddJ_adj, dddJ_adj = compute_gradient(ddJ_tlm, (zeta_3, zeta_2, kappa))


def info_compare(x, y, tol):
    info(f"{x:.16e} {y:.16e} {abs(x - y):.16e}")
    assert abs(x - y) < tol


info("TLM/adjoint consistency, zeta_1")
info_compare(dJ_tlm_1.value(), function_inner(zeta_1, dJ_adj), tol=1.0e-18)

info("TLM/adjoint consistency, zeta_2")
info_compare(dJ_tlm_2.value(), function_inner(zeta_2, dJ_adj), tol=1.0e-17)

info("Second order TLM/adjoint consistency")
info_compare(ddJ_tlm.value(), function_inner(zeta_2, ddJ_adj), tol=1.0e-17)

min_order = taylor_test_tlm(forward, kappa, tlm_order=1, seed=1.0e-3)
assert min_order > 1.99

min_order = taylor_test_tlm(forward, kappa, tlm_order=2, seed=1.0e-3)
assert min_order > 1.99

min_order = taylor_test_tlm_adjoint(forward, kappa, adjoint_order=1,
                                    seed=1.0e-3)
assert min_order > 1.99

min_order = taylor_test_tlm_adjoint(forward, kappa, adjoint_order=2,
                                    seed=1.0e-3)
assert min_order > 1.99

min_order = taylor_test_tlm_adjoint(forward, kappa, adjoint_order=3,
                                    seed=1.0e-3)
assert min_order > 1.99
