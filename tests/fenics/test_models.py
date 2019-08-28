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
from tlm_adjoint_fenics import *

from test_base import *

import copy
import pytest


def oscillator_ref():
    assert(not manager().annotation_enabled())
    assert(not manager().tlm_enabled())

    nsteps = 20
    dt = Constant(0.01)
    T_0 = Constant((1.0, 0.0))

    import mpi4py.MPI as MPI
    mesh = UnitIntervalMesh(MPI.COMM_WORLD.size)
    space = FunctionSpace(mesh, "R", 0)
    space = FunctionSpace(mesh, space.ufl_element() * space.ufl_element())
    test = TestFunction(space)

    T_n = Function(space, name="T_n")
    T_np1 = Function(space, name="T_np1")
    T_s = 0.5 * (T_n + T_np1)

    T_n.assign(T_0)
    for n in range(nsteps):
        solve(
            inner(test, (T_np1 - T_n) / dt) * dx
            - inner(test[0], T_s[1]) * dx
            + inner(test[1], sin(T_s[0])) * dx == 0,
            T_np1,
            solver_parameters={"nonlinear_solver": "newton",
                               "newton_solver": ns_parameters_newton_gmres})
        T_n, T_np1 = T_np1, T_n

    return T_n.vector().max()


def diffusion_ref():
    assert(not manager().annotation_enabled())
    assert(not manager().tlm_enabled())

    n_steps = 20
    dt = Constant(0.01)
    kappa = Constant(1.0)

    mesh = UnitIntervalMesh(100)
    space = FunctionSpace(mesh, "Lagrange", 1)
    test, trial = TestFunction(space), TrialFunction(space)

    T_n = Function(space, name="T_n")
    T_np1 = Function(space, name="T_np1")

    T_n.interpolate(Expression("sin(pi * x[0]) + sin(10.0 * pi * x[0])",
                    element=space.ufl_element()))
    for n in range(n_steps):
        solve(inner(test, trial / dt) * dx
              + inner(grad(test), kappa * grad(trial)) * dx
              == inner(test, T_n / dt) * dx,
              T_np1,
              DirichletBC(space, 1.0, "on_boundary"),
              solver_parameters=ls_parameters_gmres)
        T_n, T_np1 = T_np1, T_n

    return assemble(inner(T_n * T_n, T_n * T_n) * dx)


@pytest.mark.fenics
@pytest.mark.parametrize(
    "cp_method, cp_parameters",
    [("memory", {"replace": True}),
     ("periodic_disk", {"period": 3, "format": "pickle"}),
     ("periodic_disk", {"period": 3, "format": "hdf5"}),
     ("multistage", {"format": "pickle", "snaps_on_disk": 1, "snaps_in_ram": 2,
                     "verbose": True}),
     ("multistage", {"format": "hdf5", "snaps_on_disk": 1, "snaps_in_ram": 2,
                     "verbose": True})])
def test_oscillator(setup_test, test_leaks,
                    cp_method, cp_parameters):
    n_steps = 20
    if cp_method == "multistage":
        cp_parameters = copy.copy(cp_parameters)
        cp_parameters["blocks"] = n_steps
    configure_checkpointing(cp_method, cp_parameters)

    mesh = UnitIntervalMesh(20)
    r0 = FiniteElement("R", mesh.ufl_cell(), 0)
    space = FunctionSpace(mesh, r0 * r0)
    test = TestFunction(space)
    T_0 = Function(space, name="T_0", static=True)
    T_0.assign(Constant((1.0, 0.0)))
    dt = Constant(0.01, static=True)

    def forward(T_0):
        T_n = Function(space, name="T_n")
        T_np1 = Function(space, name="T_np1")
        T_s = 0.5 * (T_n + T_np1)

        AssignmentSolver(T_0, T_n).solve()

        solver_parameters = {"nonlinear_solver": "newton",
                             "newton_solver": ns_parameters_newton_gmres}
        eq = EquationSolver(inner(test, (T_np1 - T_n) / dt) * dx
                            - inner(test[0], T_s[1]) * dx
                            + inner(test[1], sin(T_s[0])) * dx == 0,
                            T_np1,
                            solver_parameters=solver_parameters)

        for n in range(n_steps):
            eq.solve()
            T_n.assign(T_np1)
            if n < n_steps - 1:
                new_block()

        J = Functional(name="J")
        J.assign(inner(T_n[0], T_n[0]) * dx)
        return J

    start_manager()
    J = forward(T_0)
    stop_manager()

    J_val = J.value()
    J_val_ref = oscillator_ref() ** 2
    assert(abs(J_val - J_val_ref) < 1.0e-15)

    dJ = compute_gradient(J, T_0)

    dm = Function(space, name="dm", static=True)
    function_assign(dm, 1.0)

    min_order = taylor_test(forward, T_0, J_val=J_val, dJ=dJ, dM=dm)
    assert(min_order > 2.00)

    ddJ = Hessian(forward)
    min_order = taylor_test(forward, T_0, J_val=J_val, ddJ=ddJ, dM=dm)
    assert(min_order > 3.00)

    min_order = taylor_test_tlm(forward, T_0, tlm_order=1, dMs=(dm,))
    assert(min_order > 2.00)

    min_order = taylor_test_tlm_adjoint(forward, T_0, adjoint_order=1,
                                        dMs=(dm,))
    assert(min_order > 2.00)

    min_order = taylor_test_tlm_adjoint(forward, T_0, adjoint_order=2,
                                        dMs=(dm, dm))
    assert(min_order > 2.00)


@pytest.mark.fenics
@pytest.mark.parametrize("n_steps", [1, 2, 5, 20])
def test_diffusion_timestepping(setup_test, test_leaks, test_configurations,
                                n_steps):
    configure_checkpointing("multistage",
                            {"blocks": n_steps, "snaps_on_disk": 1,
                             "snaps_in_ram": 2, "verbose": True})

    mesh = UnitIntervalMesh(100)
    X = SpatialCoordinate(mesh)
    space = FunctionSpace(mesh, "Lagrange", 1)
    test, trial = TestFunction(space), TrialFunction(space)
    T_0 = Function(space, name="T_0", static=True)
    interpolate_expression(T_0, sin(pi * X[0]) + sin(10.0 * pi * X[0]))
    dt = Constant(0.01, static=True)
    space_r0 = FunctionSpace(mesh, "R", 0)
    kappa = Function(space_r0, name="kappa", static=True)
    function_assign(kappa, 1.0)

    def forward(T_0, kappa):
        from tlm_adjoint_fenics.timestepping import N, TimeFunction, \
            TimeLevels, TimeSystem, n

        levels = TimeLevels([n, n + 1], {n: n + 1})
        T = TimeFunction(levels, space, name="T")
        T[n].rename("T_n", "a Function")
        T[n + 1].rename("T_np1", "a Function")

        system = TimeSystem()

        system.add_solve(T_0, T[0])

        system.add_solve(inner(test, trial) * dx
                         + dt * inner(grad(test), kappa * grad(trial)) * dx
                         == inner(test, T[n]) * dx,
                         T[n + 1],
                         DirichletBC(space, 1.0, "on_boundary", static=True),
                         solver_parameters=ls_parameters_cg)

        for n_step in range(n_steps):
            system.timestep()
            if n_step < n_steps - 1:
                new_block()
        system.finalize()

        J = Functional(name="J")
        J.assign(inner(T[N] * T[N], T[N] * T[N]) * dx)
        return J

    start_manager()
    J = forward(T_0, kappa)
    stop_manager()

    J_val = J.value()
    if n_steps == 20:
        J_val_ref = diffusion_ref()
        assert(abs(J_val - J_val_ref) < 1.0e-12)

    controls = [Control("T_0"), Control("kappa")]
    dJs = compute_gradient(J, controls)

    one = Function(space_r0, name="dm", static=True)
    function_assign(one, 1.0)

    for m, m0, forward_J, dJ, dm in \
            [(controls[0], T_0, lambda T_0: forward(T_0, kappa), dJs[0],
              None),
             (controls[1], kappa, lambda kappa: forward(T_0, kappa), dJs[1],
              one)]:
        min_order = taylor_test(forward_J, m, J_val=J_val, dJ=dJ, dM=dm)
        assert(min_order > 1.99)

        ddJ = Hessian(forward_J)
        min_order = taylor_test(forward_J, m, J_val=J_val, ddJ=ddJ, dM=dm)
        assert(min_order > 2.98)

        min_order = taylor_test_tlm(forward_J, m0, tlm_order=1,
                                    dMs=None if dm is None else (dm,))
        assert(min_order > 1.99)

        min_order = taylor_test_tlm_adjoint(forward_J, m0, adjoint_order=1,
                                            dMs=None if dm is None else (dm,))
        assert(min_order > 1.99)

        min_order = taylor_test_tlm_adjoint(
            forward_J, m0, adjoint_order=2,
            dMs=None if dm is None else (dm, dm), seed=1.0e-3)
        assert(min_order > 1.99)
