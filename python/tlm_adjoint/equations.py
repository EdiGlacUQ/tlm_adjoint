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

from .backend import *
from .backend_code_generator_interface import *
from .backend_interface import *

from .base_equations import AssignmentSolver, Equation, EquationException, \
    NullSolver, get_tangent_linear
from .caches import CacheRef, DirichletBC, assembly_cache, bcs_is_cached, \
    bcs_is_static, form_neg, function_is_cached, is_cached, \
    linear_solver_cache, new_count, split_action, split_form, update_caches

import copy
import operator
import numpy as np
import types
import ufl

__all__ = \
    [
        "AssembleSolver",
        "DirichletBCSolver",
        "EquationSolver",
        "ExprEvaluationSolver",
        "ProjectionSolver",
        "linear_equation_new_x"
    ]


def derivative_dependencies(expr, dep):
    dexpr = ufl.derivative(expr, dep)
    dexpr = ufl.algorithms.expand_derivatives(dexpr)
    return ufl.algorithms.extract_coefficients(dexpr)


def extract_dependencies(expr):
    deps = {}
    nl_deps = {}
    for dep in ufl.algorithms.extract_coefficients(expr):
        if is_function(dep):
            dep_id = dep.id()
            if dep_id not in deps:
                deps[dep_id] = dep
            if dep_id not in nl_deps:
                n_nl_deps = 0
                for nl_dep in derivative_dependencies(expr, dep):
                    if is_function(nl_dep):
                        nl_dep_id = nl_dep.id()
                        if nl_dep_id not in nl_deps:
                            nl_deps[nl_dep_id] = nl_dep
                        n_nl_deps += 1
                if n_nl_deps > 0 and dep_id not in nl_deps:
                    nl_deps[dep_id] = dep

    return deps, nl_deps


class AssembleSolver(Equation):
    def __init__(self, rhs, x, form_compiler_parameters={},
                 match_quadrature=None):
        if match_quadrature is None:
            match_quadrature = parameters["tlm_adjoint"]["AssembleSolver"]["match_quadrature"]  # noqa: E501

        rhs = ufl.classes.Form(rhs.integrals())

        rank = len(rhs.arguments())
        if rank == 0:
            if not is_real_function(x):
                raise EquationException("Rank 0 forms can only be assigned to real functions")  # noqa: E501
        elif rank != 1:
            raise EquationException("Must be a rank 0 or 1 form")

        deps, nl_deps = extract_dependencies(rhs)
        if x.id() in deps:
            raise EquationException("Invalid non-linear dependency")
        deps, nl_deps = list(deps.values()), tuple(nl_deps.values())
        deps.insert(0, x)

        form_compiler_parameters_ = \
            copy_parameters_dict(parameters["form_compiler"])
        update_parameters_dict(form_compiler_parameters_,
                               form_compiler_parameters)
        form_compiler_parameters = form_compiler_parameters_
        if match_quadrature:
            update_parameters_dict(
                form_compiler_parameters,
                form_form_compiler_parameters(rhs, form_compiler_parameters))

        Equation.__init__(self, x, deps, nl_deps=nl_deps, ic_deps=[])
        self._rank = rank
        self._rhs = rhs
        self._form_compiler_parameters = form_compiler_parameters

    def replace(self, replace_map):
        Equation.replace(self, replace_map)
        self._rhs = ufl.replace(self._rhs, replace_map)

    def forward_solve(self, x, deps=None):
        if deps is None:
            rhs = self._rhs
        else:
            rhs = ufl.replace(self._rhs, dict(zip(self.dependencies(), deps)))

        if self._rank == 0:
            function_assign(
                x,
                assemble(rhs,
                         form_compiler_parameters=self._form_compiler_parameters))  # noqa: E501
        else:
            assert(self._rank == 1)
            assemble(rhs,
                     form_compiler_parameters=self._form_compiler_parameters,
                     tensor=function_vector(x))

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        # Derived from EquationSolver.derivative_action (see dolfin-adjoint
        # reference below). Code first added 2017-12-07.
        # Re-written 2018-01-28
        # Updated to adjoint only form 2018-01-29

        eq_deps = self.dependencies()
        if dep_index < 0 or dep_index >= len(eq_deps):
            return None
        elif dep_index == 0:
            return adj_x

        dep = eq_deps[dep_index]
        if self._rank == 0:
            argument = TestFunction(dep.function_space())
        else:
            assert(self._rank == 1)
            argument = TrialFunction(dep.function_space())
        dF = ufl.algorithms.expand_derivatives(
            ufl.derivative(self._rhs, dep, argument=argument))
        if dF.empty():
            return None

        dF = ufl.replace(dF, dict(zip(self.nonlinear_dependencies(), nl_deps)))
        if self._rank == 0:
            dF = assemble(
                dF, form_compiler_parameters=self._form_compiler_parameters)
            return (-function_max_value(adj_x), dF)
        else:
            assert(self._rank == 1)
            dF = assemble(
                ufl.action(adjoint(dF), adj_x),
                form_compiler_parameters=self._form_compiler_parameters)
            return (-1.0, dF)

    def adjoint_jacobian_solve(self, nl_deps, b):
        return b

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        tlm_rhs = ufl.classes.Zero()
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    tlm_rhs += ufl.derivative(self._rhs, dep, argument=tau_dep)

        if isinstance(tlm_rhs, ufl.classes.Zero):
            return NullSolver(tlm_map[x])
        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if tlm_rhs.empty():
            return NullSolver(tlm_map[x])
        else:
            return AssembleSolver(
                tlm_rhs, tlm_map[x],
                form_compiler_parameters=self._form_compiler_parameters)


class FunctionAlias(backend_Function):
    def __init__(self, space, x=None):
        ufl.classes.Coefficient.__init__(self, space, count=new_count())
        self.__base_keys = set(self.__dict__.keys())
        self.__base_keys.add("_FunctionAlias__base_keys")
        if x is not None:
            self._alias(x)

    def _alias(self, x):
        self._clear()

        for key, value in x.__dict__.items():
            if key not in self.__base_keys:
                self.__dict__[key] = value

        ufl.log.push_level(ufl.log.CRITICAL)
        for key in dir(x):
            if key in self.__base_keys:
                continue

            try:
                value = getattr(x, key)
                has_key = True
            # Can occur with Firedrake
            except (AttributeError, ufl.UFLException):
                has_key = False
            if not has_key:
                continue

            if isinstance(value, types.MethodType):
                self.__dict__[key] = types.MethodType(value.__func__, self)
        ufl.log.pop_level()

    def _clear(self):
        for key in tuple(self.__dict__.keys()):
            if key not in self.__base_keys:
                del(self.__dict__[key])


def alias_form(form, deps):
    adeps = tuple(FunctionAlias(dep.function_space()) for dep in deps)
    return_value = ufl.replace(form, dict(zip(deps, adeps)))
    assert("_tlm_adjoint__adeps" not in return_value._cache)
    return_value._cache["_tlm_adjoint__adeps"] = adeps
    return return_value


def alias_replace(form, deps):
    for adep, dep in zip(form._cache["_tlm_adjoint__adeps"], deps):
        adep._alias(dep)


def alias_clear(form):
    for adep in form._cache["_tlm_adjoint__adeps"]:
        adep._clear()


def alias_assemble(form, deps, *args, **kwargs):
    alias_replace(form, deps)
    return_value = assemble(form, *args, **kwargs)
    alias_clear(form)
    return return_value


def homogenized_bc(bc):
    if hasattr(bc, "is_homogeneous") and bc.is_homogeneous():
        return bc
    else:
        hbc = homogenize(bc)
        static = bcs_is_static([bc])
        hbc.is_static = lambda: static
        cache = bcs_is_cached([bc])
        hbc.is_cached = lambda: cache
        hbc.is_homogeneous = lambda: True
        hbc.homogenize = lambda: hbc
        return hbc


class EquationSolver(Equation):
    # eq, x, bcs, J, form_compiler_parameters and solver_parameters argument
    # usage based on the interface for the solve function in FEniCS (see e.g.
    # FEniCS 2017.1.0)
    def __init__(self, eq, x, bcs=[], J=None, form_compiler_parameters={},
                 solver_parameters={}, adjoint_solver_parameters=None,
                 tlm_solver_parameters=None, initial_guess=None,
                 cache_jacobian=None, cache_adjoint_jacobian=None,
                 cache_tlm_jacobian=None, cache_rhs_assembly=None,
                 match_quadrature=None, defer_adjoint_assembly=None):
        if isinstance(bcs, backend_DirichletBC):
            bcs = [bcs]
        if cache_jacobian is None:
            if not parameters["tlm_adjoint"]["EquationSolver"]["enable_jacobian_caching"]:  # noqa: E501
                cache_jacobian = False
        if cache_rhs_assembly is None:
            cache_rhs_assembly = parameters["tlm_adjoint"]["EquationSolver"]["cache_rhs_assembly"]  # noqa: E501
        if match_quadrature is None:
            match_quadrature = parameters["tlm_adjoint"]["EquationSolver"]["match_quadrature"]  # noqa: E501
        if defer_adjoint_assembly is None:
            defer_adjoint_assembly = parameters["tlm_adjoint"]["EquationSolver"]["defer_adjoint_assembly"]  # noqa: E501
        if match_quadrature and defer_adjoint_assembly:
            raise EquationException("Cannot both match quadrature and defer adjoint assembly")  # noqa: E501

        lhs, rhs = eq.lhs, eq.rhs
        del(eq)
        lhs = ufl.classes.Form(lhs.integrals())
        linear = isinstance(rhs, ufl.classes.Form)
        if linear:
            rhs = ufl.classes.Form(rhs.integrals())
        if J is not None:
            J = ufl.classes.Form(J.integrals())

        if linear:
            if x in lhs.coefficients() or x in rhs.coefficients():
                raise EquationException("Invalid non-linear dependency")
            F = ufl.action(lhs, coefficient=x) + form_neg(rhs)
            nl_solve_J = None
            J = lhs
        else:
            F = lhs
            if rhs != 0:
                raise EquationException("Invalid right-hand-side")
            nl_solve_J = J
            J = ufl.algorithms.expand_derivatives(ufl.derivative(
                F, x, argument=TrialFunction(x.function_space())))

        deps, nl_deps = extract_dependencies(F)

        if nl_solve_J is not None:
            for dep in nl_solve_J.coefficients():
                if is_function(dep):
                    dep_id = dep.id()
                    if dep_id not in deps:
                        deps[dep_id] = dep

        if initial_guess == x:
            initial_guess = None
        if initial_guess is not None:
            initial_guess_id = initial_guess.id()
            if initial_guess_id not in deps:
                deps[initial_guess_id] = initial_guess

        deps = list(deps.values())
        if x in deps:
            deps.remove(x)
        deps.insert(0, x)
        nl_deps = tuple(nl_deps.values())

        hbcs = [homogenized_bc(bc) for bc in bcs]

        if cache_jacobian is None:
            cache_jacobian = is_cached(J) and bcs_is_cached(bcs)
        if cache_adjoint_jacobian is None:
            cache_adjoint_jacobian = cache_jacobian
        if cache_tlm_jacobian is None:
            cache_tlm_jacobian = cache_jacobian

        if nl_solve_J is None:
            (solver_parameters,
             linear_solver_parameters,
             checkpoint_ic) = process_solver_parameters(
                solver_parameters, J, linear)
        else:
            _, linear_solver_parameters, _ = process_solver_parameters(
                solver_parameters, J, linear)
            solver_parameters, _, checkpoint_ic = process_solver_parameters(
                solver_parameters, nl_solve_J, linear)

        if adjoint_solver_parameters is None:
            adjoint_solver_parameters = process_adjoint_solver_parameters(
                linear_solver_parameters)
        else:
            _, adjoint_solver_parameters, _ = process_solver_parameters(
                adjoint_solver_parameters, J, linear=True)

        if tlm_solver_parameters is not None:
            _, tlm_solver_parameters, _ = process_solver_parameters(
                tlm_solver_parameters, J, linear=True)

        if initial_guess is None and checkpoint_ic:
            ic_deps = [x]
        else:
            ic_deps = []

        form_compiler_parameters_ = copy_parameters_dict(parameters["form_compiler"])  # noqa: E501
        update_parameters_dict(form_compiler_parameters_,
                               form_compiler_parameters)
        form_compiler_parameters = form_compiler_parameters_
        if match_quadrature:
            update_parameters_dict(
                form_compiler_parameters,
                form_form_compiler_parameters(F, form_compiler_parameters))

        Equation.__init__(self, x, deps, nl_deps=nl_deps, ic_deps=ic_deps)
        self._F = F
        self._lhs, self._rhs = lhs, rhs
        self._bcs = list(bcs)
        self._hbcs = hbcs
        self._J = J
        self._nl_solve_J = nl_solve_J
        self._form_compiler_parameters = form_compiler_parameters
        self._solver_parameters = solver_parameters
        self._linear_solver_parameters = linear_solver_parameters
        self._adjoint_solver_parameters = adjoint_solver_parameters
        self._tlm_solver_parameters = tlm_solver_parameters
        if initial_guess is None:
            self._initial_guess_index = None
        else:
            self._initial_guess_index = deps.index(initial_guess)
        self._linear = linear

        self._cache_jacobian = cache_jacobian
        self._cache_adjoint_jacobian = cache_adjoint_jacobian
        self._cache_tlm_jacobian = cache_tlm_jacobian
        self._cache_rhs_assembly = cache_rhs_assembly
        self._defer_adjoint_assembly = defer_adjoint_assembly

        self._forward_eq = None
        self._forward_J_mat = CacheRef()
        self._forward_J_solver = CacheRef()
        self._forward_b_pa = None

        self._derivative_mats = {}

        self._adjoint_J_solver = CacheRef()
        self._adjoint_J = None

    def replace(self, replace_map):
        Equation.replace(self, replace_map)

        self._F = ufl.replace(self._F, replace_map)
        self._lhs = ufl.replace(self._lhs, replace_map)
        if self._rhs != 0:
            self._rhs = ufl.replace(self._rhs, replace_map)
        self._J = ufl.replace(self._J, replace_map)
        if self._nl_solve_J is not None:
            self._nl_solve_J = ufl.replace(self._nl_solve_J, replace_map)

        if self._forward_b_pa is not None:
            if self._forward_b_pa[0] is not None:
                self._forward_b_pa[0][0] = \
                    ufl.replace(self._forward_b_pa[0][0], replace_map)
            for dep_index, (mat_form,
                            mat_cache) in self._forward_b_pa[1].items():
                self._forward_b_pa[1][dep_index][0] = \
                    ufl.replace(mat_form, replace_map)

        if self._defer_adjoint_assembly:
            for dep_index, mat_cache in self._derivative_mats.items():
                if isinstance(mat_cache, ufl.classes.Form):
                    self._derivative_mats[dep_index] = \
                        ufl.replace(mat_cache, replace_map)

    def _cached_rhs(self, deps, b_bc=None):
        eq_deps = self.dependencies()

        if self._forward_b_pa is None:
            # Split into cached and non-cached components
            cached_form, non_cached_form = split_form(self._rhs)

            mat_forms = {}
            if not non_cached_form.empty():
                for dep_index, dep in enumerate(eq_deps):
                    mat_form, non_cached_form = split_action(non_cached_form,
                                                             dep)
                    if not mat_form.empty():
                        # The non-cached part contains a component which can be
                        # represented as the action of a cached matrix ...
                        if function_is_cached(dep):
                            # ... on a cached dependency. This is part of the
                            # cached component.
                            cached_form += ufl.action(mat_form,
                                                      coefficient=dep)
                        else:
                            # ... on a non-cached dependency.
                            mat_forms[dep_index] = [mat_form, CacheRef()]
                    if non_cached_form.empty():
                        break

            if not non_cached_form.empty():
                # Attempt to split the remaining non-cached component into
                # cached and non-cached components
                cached_form_term, non_cached_form = split_form(non_cached_form)
                cached_form += cached_form_term

            if non_cached_form.empty():
                non_cached_form = None
            else:
                non_cached_form = alias_form(non_cached_form, eq_deps)

            if cached_form.empty():
                cached_form = None
            else:
                cached_form = [cached_form, CacheRef()]

            self._forward_b_pa = [cached_form, mat_forms, non_cached_form]
        else:
            cached_form, mat_forms, non_cached_form = self._forward_b_pa

        b = None

        if non_cached_form is not None:
            b = alias_assemble(
                non_cached_form, eq_deps if deps is None else deps,
                form_compiler_parameters=self._form_compiler_parameters)

        for dep_index, (mat_form, mat_cache) in mat_forms.items():
            mat_bc = mat_cache()
            if mat_bc is None:
                mat_forms[dep_index][1], mat_bc = assembly_cache().assemble(
                    mat_form,
                    form_compiler_parameters=self._form_compiler_parameters,
                    solver_parameters=self._linear_solver_parameters,
                    replace_map=None if deps is None else dict(zip(eq_deps,
                                                                   deps)))
            mat, _ = mat_bc
            dep = (eq_deps if deps is None else deps)[dep_index]
            if b is None:
                b = matrix_multiply(mat, function_vector(dep))
            else:
                matrix_multiply(mat, function_vector(dep), tensor=b,
                                addto=True)

        if cached_form is not None:
            cached_b = cached_form[1]()
            if cached_b is None:
                cached_form[1], cached_b = assembly_cache().assemble(
                    cached_form[0],
                    form_compiler_parameters=self._form_compiler_parameters,
                    replace_map=None if deps is None else dict(zip(eq_deps,
                                                                   deps)))
            if b is None:
                b = rhs_copy(cached_b)
            else:
                rhs_addto(b, cached_b)

        if b is None:
            raise EquationException("Empty right-hand-side")

        apply_rhs_bcs(b, self._hbcs, b_bc=b_bc)
        return b

    def forward_solve(self, x, deps=None):
        eq_deps = self.dependencies()
        update_caches(eq_deps, deps=deps)

        if self._initial_guess_index is not None:
            if deps is None:
                initial_guess = eq_deps[self._initial_guess_index]
            else:
                initial_guess = deps[self._initial_guess_index]
            function_assign(x, initial_guess)

        if self._linear:
            alias_clear_J, alias_clear_rhs = False, False
            if self._cache_jacobian:
                # Cases 1 and 2: Linear, Jacobian cached, with or without RHS
                # assembly caching

                J_mat_bc = self._forward_J_mat()
                if J_mat_bc is None:
                    # Assemble and cache the Jacobian
                    self._forward_J_mat, J_mat_bc = assembly_cache().assemble(
                        self._J, bcs=self._bcs,
                        form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                        solver_parameters=self._linear_solver_parameters,
                        replace_map=None if deps is None
                                         else dict(zip(eq_deps, deps)))
                J_mat, b_bc = J_mat_bc

                if self._cache_rhs_assembly:
                    # Assemble the RHS with RHS assembly caching
                    b = self._cached_rhs(deps, b_bc=b_bc)
                else:
                    # Assemble the RHS without RHS assembly caching
                    if deps is None:
                        rhs = self._rhs
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = (None,
                                                None,
                                                alias_form(self._rhs, eq_deps))
                        _, _, rhs = self._forward_eq
                        alias_replace(rhs, deps)
                        alias_clear_rhs = True
                    b = assemble(
                        rhs,
                        form_compiler_parameters=self._form_compiler_parameters)  # noqa: E501

                    # Add bc RHS terms
                    apply_rhs_bcs(b, self._hbcs, b_bc=b_bc)

                J_solver = self._forward_J_solver()
                if J_solver is None:
                    # Construct and cache the linear solver
                    self._forward_J_solver, J_solver = \
                        linear_solver_cache().linear_solver(
                            self._J, J_mat, bcs=self._bcs,
                            form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                            linear_solver_parameters=self._linear_solver_parameters)  # noqa: E501
            else:
                if self._cache_rhs_assembly:
                    # Case 3: Linear, Jacobian not cached, with RHS assembly
                    # caching

                    # Assemble the Jacobian
                    if deps is None:
                        J = self._J
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = (None,
                                                alias_form(self._J, eq_deps),
                                                None)
                        _, J, _ = self._forward_eq
                        alias_replace(J, deps)
                        alias_clear_J = True
                    J_mat, b_bc = assemble_matrix(
                        J, self._bcs, force_evaluation=False,
                        **assemble_arguments(2, self._form_compiler_parameters,
                                             self._linear_solver_parameters))

                    # Assemble the RHS with RHS assembly caching
                    b = self._cached_rhs(deps, b_bc=b_bc)
                else:
                    # Case 4: Linear, Jacobian not cached, without RHS assembly
                    # caching

                    # Assemble the Jacobian and RHS
                    if deps is None:
                        J, rhs = self._J, self._rhs
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = (None,
                                                alias_form(self._J, eq_deps),
                                                alias_form(self._rhs, eq_deps))
                        _, J, rhs = self._forward_eq
                        alias_replace(J, deps)
                        alias_clear_J = True
                        alias_replace(rhs, deps)
                        alias_clear_rhs = True
                    J_mat, b = assemble_system(
                        J, rhs, bcs=self._bcs,
                        **assemble_arguments(2, self._form_compiler_parameters,
                                             self._linear_solver_parameters))

                # Construct the linear solver
                J_solver = linear_solver(J_mat, self._linear_solver_parameters)

#             if deps is None:
#                 assemble_J = self._J
#                 assemble_rhs = self._rhs
#             else:
#                 assemble_J = ufl.replace(self._J, dict(zip(eq_deps, deps)))
#                 assemble_rhs = ufl.replace(self._rhs,
#                                            dict(zip(eq_deps, deps)))
#
#             # FEniCS
#             J_mat_debug, b_debug = backend_assemble_system(
#                 assemble_J, assemble_rhs, self._bcs,
#                 **assemble_arguments(2,
#                                      self._form_compiler_parameters,
#                                      self._linear_solver_parameters))
#             assert((J_mat - J_mat_debug).norm("linf")
#                    <= 1.0e-15 * J_mat.norm("linf"))
#             assert((b - b_debug).norm("linf") <= 1.0e-14 * b.norm("linf"))
#
#             # Firedrake
#             J_mat_debug = backend_assemble(
#                 assemble_J, bcs=self._bcs,
#                 **assemble_arguments(2,
#                                      self._form_compiler_parameters,
#                                      self._linear_solver_parameters))
#             b_debug = backend_assemble(
#                 assemble_rhs,
#                 form_compiler_parameters=self._form_compiler_parameters)
#             J_mat.force_evaluation()
#             J_mat_debug.force_evaluation()
#             J_error = J_mat.petscmat.copy()
#             J_error.axpy(-1.0, J_mat_debug.petscmat)
#             import petsc4py.PETSc as PETSc
#             assert(J_error.norm(norm_type=PETSc.NormType.NORM_INFINITY)
#                    <= 1.0e-15 * J_mat.petscmat.norm(norm_type=PETSc.NormType.NORM_INFINITY))  # noqa: E501
#             b_error = function_copy(b)
#             function_axpy(b_error, -1.0, b_debug)
#             assert(function_linf_norm(b_error)
#                    <= 1.0e-14 * function_linf_norm(b))

            J_solver.solve(function_vector(x), b)
            if alias_clear_J:
                alias_clear(J)
            if alias_clear_rhs:
                alias_clear(rhs)
        else:
            # Case 5: Non-linear
            assert(self._rhs == 0)
            if deps is None:
                lhs = self._lhs
                if self._nl_solve_J is None:
                    J = self._J
                else:
                    J = self._nl_solve_J
            else:
                if self._forward_eq is None:
                    self._forward_eq = \
                        (alias_form(self._lhs, eq_deps),
                         alias_form(self._J if self._nl_solve_J is None
                                    else self._nl_solve_J,
                                    eq_deps),
                         0)
                lhs, J, _ = self._forward_eq
                alias_replace(lhs, deps)
                alias_replace(J, deps)
            solve(lhs == 0, x, self._bcs, J=J,
                  form_compiler_parameters=self._form_compiler_parameters,
                  solver_parameters=self._solver_parameters)
            if deps is not None:
                alias_clear(lhs)
                alias_clear(J)

    def initialize_adjoint(self, J, nl_deps):
        update_caches(self.nonlinear_dependencies(), deps=nl_deps)

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        # Similar to 'RHS.derivative_action' and 'RHS.second_derivative_action'
        # in dolfin-adjoint file dolfin_adjoint/adjrhs.py (see e.g.
        # dolfin-adjoint version 2017.1.0)
        # Code first added to JRM personal repository 2016-05-22
        # Code first added to dolfin_adjoint_custom repository 2016-06-02
        # Re-written 2018-01-28

        eq_deps = self.dependencies()
        if dep_index < 0 or dep_index >= len(eq_deps):
            return None

        if dep_index in self._derivative_mats:
            mat_cache = self._derivative_mats[dep_index]
            if mat_cache is None:
                return None
            elif isinstance(mat_cache, CacheRef):
                mat_bc = mat_cache()
                if mat_bc is not None:
                    mat, _ = mat_bc
                    return matrix_multiply(mat, function_vector(adj_x))
                # else:
                #   # Cache entry cleared
                #   pass
            elif self._defer_adjoint_assembly:
                assert(isinstance(mat_cache, ufl.classes.Form))
                return ufl.action(
                    ufl.replace(
                        mat_cache,
                        dict(zip(self.nonlinear_dependencies(), nl_deps))),
                    coefficient=adj_x)
            else:
                assert(isinstance(mat_cache, ufl.classes.Form))
                return alias_assemble(
                    mat_cache, list(nl_deps) + [adj_x],
                    form_compiler_parameters=self._form_compiler_parameters)

        dep = eq_deps[dep_index]
        dF = ufl.algorithms.expand_derivatives(ufl.derivative(
            self._F, dep, argument=TrialFunction(dep.function_space())))
        if dF.empty():
            self._derivative_mats[dep_index] = None
            return None
        dF = adjoint(dF)

        if self._cache_rhs_assembly and is_cached(dF):
            self._derivative_mats[dep_index], (mat, _) = \
                assembly_cache().assemble(
                    dF,
                    form_compiler_parameters=self._form_compiler_parameters,
                    replace_map=dict(zip(self.nonlinear_dependencies(),
                                         nl_deps)))
            return matrix_multiply(mat, function_vector(adj_x))
        elif self._defer_adjoint_assembly:
            self._derivative_mats[dep_index] = dF
            dF = ufl.replace(dF, dict(zip(self.nonlinear_dependencies(),
                                          nl_deps)))
            return ufl.action(dF, coefficient=adj_x)
        else:
            dF = alias_form(
                ufl.action(dF, coefficient=adj_x),
                list(self.nonlinear_dependencies()) + [adj_x])
            self._derivative_mats[dep_index] = dF
            return alias_assemble(
                dF, list(nl_deps) + [adj_x],
                form_compiler_parameters=self._form_compiler_parameters)

    def adjoint_jacobian_solve(self, nl_deps, b):
        if self._cache_adjoint_jacobian:
            J_solver = self._adjoint_J_solver()
            if J_solver is None:
                J = adjoint(self._J)
                _, (J_mat, _) = assembly_cache().assemble(
                    J, bcs=self._hbcs,
                    form_compiler_parameters=self._form_compiler_parameters,
                    solver_parameters=self._adjoint_solver_parameters,
                    replace_map=dict(zip(self.nonlinear_dependencies(),
                                         nl_deps)))
                self._adjoint_J_solver, J_solver = \
                    linear_solver_cache().linear_solver(
                        J, J_mat, bcs=self._hbcs,
                        form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                        linear_solver_parameters=self._adjoint_solver_parameters)  # noqa: E501

            apply_rhs_bcs(function_vector(b), self._hbcs)
            adj_x = function_new(b)
            J_solver.solve(function_vector(adj_x), function_vector(b))

            return adj_x
        else:
            if self._adjoint_J is None:
                self._adjoint_J = alias_form(
                    adjoint(self._J), self.nonlinear_dependencies())
            alias_replace(self._adjoint_J, nl_deps)
            J_mat, _ = assemble_matrix(
                self._adjoint_J, self._hbcs,
                force_evaluation=False,
                **assemble_arguments(2, self._form_compiler_parameters,
                                     self._adjoint_solver_parameters))

            J_solver = linear_solver(J_mat, self._adjoint_solver_parameters)

            apply_rhs_bcs(function_vector(b), self._hbcs)
            adj_x = function_new(b)
            J_solver.solve(function_vector(adj_x),
                           function_vector(b))
            alias_clear(self._adjoint_J)

            return adj_x

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        tlm_rhs = ufl.classes.Zero()
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    tlm_rhs += form_neg(ufl.derivative(self._F, dep,
                                                       argument=tau_dep))

        if isinstance(tlm_rhs, ufl.classes.Zero):
            return NullSolver(tlm_map[x])
        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if tlm_rhs.empty():
            return NullSolver(tlm_map[x])
        else:
            if self._tlm_solver_parameters is None:
                tlm_solver_parameters = self._linear_solver_parameters
            else:
                tlm_solver_parameters = self._tlm_solver_parameters
            if self._initial_guess_index is None:
                tlm_initial_guess = None
            else:
                initial_guess = self.dependencies()[self._initial_guess_index]
                tlm_initial_guess = tlm_map[initial_guess]
            return EquationSolver(
                self._J == tlm_rhs, tlm_map[x], self._hbcs,
                form_compiler_parameters=self._form_compiler_parameters,
                solver_parameters=tlm_solver_parameters,
                adjoint_solver_parameters=self._adjoint_solver_parameters,
                tlm_solver_parameters=tlm_solver_parameters,
                initial_guess=tlm_initial_guess,
                cache_jacobian=self._cache_tlm_jacobian,
                cache_adjoint_jacobian=self._cache_adjoint_jacobian,
                cache_tlm_jacobian=self._cache_tlm_jacobian,
                cache_rhs_assembly=self._cache_rhs_assembly,
                defer_adjoint_assembly=self._defer_adjoint_assembly)


def linear_equation_new_x(eq, x, manager=None, annotate=None, tlm=None):
    lhs, rhs = eq.lhs, eq.rhs
    lhs_x_dep = x in lhs.coefficients()
    rhs_x_dep = x in rhs.coefficients()
    if lhs_x_dep or rhs_x_dep:
        x_old = function_new(x)
        AssignmentSolver(x, x_old).solve(manager=manager, annotate=annotate,
                                         tlm=tlm)
        if lhs_x_dep:
            lhs = ufl.replace(lhs, {x: x_old})
        if rhs_x_dep:
            rhs = ufl.replace(rhs, {x: x_old})
        return lhs == rhs
    else:
        return eq


class ProjectionSolver(EquationSolver):
    def __init__(self, rhs, x, *args, **kwargs):
        space = x.function_space()
        test, trial = TestFunction(space), TrialFunction(space)
        if not isinstance(rhs, ufl.classes.Form):
            rhs = ufl.inner(test, rhs) * ufl.dx
        EquationSolver.__init__(self, ufl.inner(test, trial) * ufl.dx == rhs,
                                x, *args, **kwargs)


class DirichletBCSolver(Equation):
    def __init__(self, y, x, *args, **kwargs):
        Equation.__init__(self, x, [x, y], nl_deps=[], ic_deps=[])
        self._bc_args = copy.copy(args)
        self._bc_kwargs = copy.copy(kwargs)

    def forward_solve(self, x, deps=None):
        _, y = self.dependencies() if deps is None else deps
        function_zero(x)
        DirichletBC(
            x.function_space(), y,
            *self._bc_args, **self._bc_kwargs).apply(function_vector(x))

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        if dep_index == 0:
            return adj_x
        elif dep_index == 1:
            _, y = self.dependencies()
            F = function_new(y)
            DirichletBC(
                y.function_space(), adj_x,
                *self._bc_args, **self._bc_kwargs).apply(function_vector(F))
            return (-1.0, F)
        else:
            return None

    def adjoint_jacobian_solve(self, nl_deps, b):
        return b

    def tangent_linear(self, M, dM, tlm_map):
        x, y = self.dependencies()

        tau_y = get_tangent_linear(y, M, dM, tlm_map)
        if tau_y is None:
            return NullSolver(tlm_map[x])
        else:
            return DirichletBCSolver(tau_y, tlm_map[x],
                                     *self._bc_args, **self._bc_kwargs)


def evaluate_expr_binary_operator(fn):
    def evaluate_expr_binary_operator(x):
        x_0, x_1 = map(evaluate_expr, x.ufl_operands)
        return fn(x_0, x_1)
    return evaluate_expr_binary_operator


def evaluate_expr_function(fn):
    def evaluate_expr_function(x):
        x_0, = map(evaluate_expr, x.ufl_operands)
        return fn(x_0)
    return evaluate_expr_function


evaluate_expr_types = \
    {
        ufl.classes.FloatValue: (lambda x: float(x)),
        ufl.classes.IntValue: (lambda x: float(x)),
        ufl.classes.Zero: (lambda x: 0.0),
    }

for ufl_name, op_name in [("Division", "truediv"),
                          ("Power", "pow"),
                          ("Product", "mul"),
                          ("Sum", "add")]:
    evaluate_expr_types[getattr(ufl.classes, ufl_name)] \
        = evaluate_expr_binary_operator(getattr(operator, op_name))
del(ufl_name, op_name)

for ufl_name, numpy_name in [("Abs", "abs"),
                             ("Acos", "arccos"),
                             ("Asin", "arcsin"),
                             ("Atan", "arctan"),
                             ("Atan2", "arctan2"),
                             ("Cos", "cos"),
                             ("Cosh", "cosh"),
                             ("Exp", "exp"),
                             ("Ln", "log"),
                             ("MaxValue", "max"),
                             ("MinValue", "min"),
                             ("Sin", "sin"),
                             ("Sinh", "sinh"),
                             ("Sqrt", "sqrt"),
                             ("Tan", "tan"),
                             ("Tanh", "tanh")]:
    evaluate_expr_types[getattr(ufl.classes, ufl_name)] \
        = evaluate_expr_function(getattr(np, numpy_name))
del(ufl_name, numpy_name)


def evaluate_expr(x):
    if is_function(x):
        if is_real_function(x):
            return function_max_value(x)
        else:
            return function_get_values(x)
    elif isinstance(x, backend_Constant):
        return float(x)
    else:
        return evaluate_expr_types[type(x)](x)


class ExprEvaluationSolver(Equation):
    def __init__(self, rhs, x):
        if isinstance(rhs, ufl.classes.Form):
            raise EquationException("rhs should not be a Form")
        x_space = x.function_space()
        if len(x_space.ufl_element().value_shape()) > 0:
            raise EquationException("Solution must be a scalar Function")

        deps, nl_deps = extract_dependencies(rhs)
        deps, nl_deps = list(deps.values()), tuple(nl_deps.values())
        x_space_id = function_space_id(x_space)
        for dep in deps:
            if dep == x:
                raise EquationException("Invalid non-linear dependency")
            elif function_space_id(dep.function_space()) != x_space_id and \
                    not is_real_function(dep):
                raise EquationException("Invalid function space")
        deps.insert(0, x)

        Equation.__init__(self, x, deps, nl_deps=nl_deps, ic_deps=[])
        # Store the Expr in a Form to aid caching
        self._rhs = rhs * ufl.dx(x_space.mesh())

        self._forward_rhs = None

        self._adjoint_derivatives = {}

    def replace(self, replace_map):
        Equation.replace(self, replace_map)
        self._rhs = ufl.replace(self._rhs, replace_map)

    def forward_solve(self, x, deps=None):
        if deps is None:
            rhs = self._rhs
        else:
            if self._forward_rhs is None:
                self._forward_rhs = alias_form(self._rhs, self.dependencies())
            rhs = self._forward_rhs
            alias_replace(rhs, deps)
        rhs_val = evaluate_expr(rhs.integrals()[0].integrand())
        if deps is not None:
            alias_clear(rhs)
        if isinstance(rhs_val, float):
            function_assign(x, rhs_val)
        else:
            assert(function_local_size(x) == len(rhs_val))
            function_set_values(x, rhs_val)

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        if dep_index == 0:
            return adj_x
        else:
            dep = self.dependencies()[dep_index]
            if dep_index in self._adjoint_derivatives:
                dF = self._adjoint_derivatives[dep_index]
            else:
                dF = self._adjoint_derivatives[dep_index] \
                    = alias_form(
                        ufl.algorithms.expand_derivatives(
                            ufl.derivative(self._rhs, dep, argument=adj_x)),
                        list(self.nonlinear_dependencies()) + [adj_x])
            alias_replace(dF, list(nl_deps) + [adj_x])
            dF_val = evaluate_expr(dF.integrals()[0].integrand())
            alias_clear(dF)
            F = function_new(dep)
            if isinstance(dF_val, float):
                function_assign(F, dF_val)
            elif is_real_function(F):
                dF_val_local = np.array([dF_val.sum()], dtype=np.float64)
                dF_val = np.empty((1,), dtype=np.float64)
                import mpi4py.MPI as MPI
                comm = function_comm(F)
                # FEniCS backwards compatibility
                if hasattr(comm, "tompi4py"):
                    comm = comm.tompi4py()
                comm.Allreduce(dF_val_local, dF_val, op=MPI.SUM)
                dF_val = dF_val[0]
                function_assign(F, dF_val)
            else:
                assert(function_local_size(F) == len(dF_val))
                function_set_values(F, dF_val)
            return (-1.0, F)

    def adjoint_jacobian_solve(self, nl_deps, b):
        return b

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        rhs = self._rhs.integrals()[0].integrand()
        tlm_rhs = ufl.classes.Zero()
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    tlm_rhs += ufl.derivative(rhs, dep, argument=tau_dep)

        if isinstance(tlm_rhs, ufl.classes.Zero):
            return NullSolver(tlm_map[x])
        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if isinstance(tlm_rhs, ufl.classes.Zero):
            return NullSolver(tlm_map[x])
        else:
            return ExprEvaluationSolver(tlm_rhs, tlm_map[x])
