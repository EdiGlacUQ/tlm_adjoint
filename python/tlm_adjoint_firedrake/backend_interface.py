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
from .backend_code_generator_interface import copy_parameters_dict, \
    is_real_function

from .caches import Function, ReplacementFunction, clear_caches, form_neg, \
    function_is_cached, function_is_checkpointed, function_is_static, \
    function_state, function_tlm_depth, function_update_state, is_function, \
    replaced_function

import ufl
import sys

__all__ = \
    [
        "Function",
        "FunctionSpace",
        "RealFunctionSpace",
        "ReplacementFunction",
        "clear_caches",
        "copy_parameters_dict",
        "default_comm",
        "finalize_adjoint_derivative_action",
        "function_alias",
        "function_assign",
        "function_axpy",
        "function_comm",
        "function_copy",
        "function_get_values",
        "function_global_size",
        "function_inner",
        "function_is_cached",
        "function_is_checkpointed",
        "function_is_static",
        "function_linf_norm",
        "function_local_indices",
        "function_local_size",
        "function_max_value",
        "function_new",
        "function_set_values",
        "function_space_id",
        "function_state",
        "function_sum",
        "function_tangent_linear",
        "function_tlm_depth",
        "function_update_state",
        "function_zero",
        "info",
        "is_function",
        "replaced_function",
        "subtract_adjoint_derivative_action",
        "warning"
    ]


# def clear_caches(*deps):


def info(message):
    sys.stdout.write(f"{message:s}\n")
    sys.stdout.flush()


def warning(message):
    sys.stderr.write(f"{message:s}\n")
    sys.stderr.flush()


# def copy_parameters_dict(parameters):


# class FunctionSpace:


def function_space_id(space):
    return id(space)


def RealFunctionSpace(comm=None):
    if comm is None:
        comm = default_comm()
    return FunctionSpace(UnitIntervalMesh(comm.size, comm=comm), "R", 0)


# class Function:
#     def __init__(self, space, name=None, static=False, cache=None,
#                  checkpoint=None, tlm_depth=0):
#     def function_space(self):
#     def id(self):
#     def name(self):


backend_Function.id = lambda self: self.count()


# class ReplacementFunction:
#     def __init__(self, x):
#     def function_space(self):
#     def id(self):
#     def name(self):


# def replaced_function(x):


# def is_function(x):


# def function_state(x):


# def function_update_state(*X):


# def function_is_static(x):


# def function_is_cached(x):


# def function_is_checkpointed(x):


# def function_tlm_depth(x):


def function_copy(x, name=None, static=False, cache=None, checkpoint=None,
                  tlm_depth=0):
    y = x.copy(deepcopy=True)
    if name is not None:
        y.rename(name, "a Function")
    y.is_static = lambda: static
    if cache is None:
        cache = static
    y.is_cached = lambda: cache
    if checkpoint is None:
        checkpoint = not static
    y.is_checkpointed = lambda: checkpoint
    y.tlm_depth = lambda: tlm_depth
    return y


def function_assign(x, y):
    if isinstance(y, (int, float)):
        if is_real_function(x):
            # Work around Firedrake issue #1459
            x.vector()[:] = float(y)
        else:
            with x.vector().dat.vec_wo as x_v:
                x_v.set(float(y))
    else:
        if is_real_function(x):
            # Work around Firedrake bug (related to issue #1459?)
            function_set_values(x, function_get_values(y))
        else:
            with x.vector().dat.vec_wo as x_v, y.vector().dat.vec_ro as y_v:
                y_v.copy(result=x_v)


def function_axpy(x, alpha, y):
    if is_real_function(x):
        # Work around Firedrake bug (related to issue #1459?)
        function_set_values(x,
                            function_get_values(x)
                            + alpha * function_get_values(y))
    else:
        # Work around Firedrake bug #1276
        with x.vector().dat.vec as x_v, y.vector().dat.vec_ro as y_v:
            x_v.axpy(alpha, y_v)


def default_comm():
    import mpi4py.MPI as MPI
    return MPI.COMM_WORLD


def function_comm(x):
    return x.comm


def function_inner(x, y):
    return x.vector().inner(y.vector())


def function_local_size(x):
    local_range = x.vector().local_range()
    return local_range[1] - local_range[0]


def function_get_values(x):
    return x.vector().get_local()


def function_set_values(x, values):
    x.vector().set_local(values)


def function_max_value(x):
    return x.vector().max()


def function_sum(x):
    return x.vector().sum()


def function_linf_norm(x):
    import petsc4py.PETSc as PETSc
    with x.vector().dat.vec_ro as x_v:
        linf_norm = x_v.norm(norm_type=PETSc.NormType.NORM_INFINITY)
    return linf_norm


def function_new(x, name=None, static=False, cache=None, checkpoint=None,
                 tlm_depth=0):
    return Function(x.function_space(), name=name, static=static,
                    cache=cache, checkpoint=checkpoint, tlm_depth=tlm_depth)


def function_tangent_linear(x, name=None):
    if hasattr(x, "tangent_linear"):
        return x.tangent_linear(name=name)
    elif function_is_static(x):
        return None
    else:
        return function_new(x, name=name, static=False,
                            cache=function_is_cached(x),
                            checkpoint=function_is_checkpointed(x),
                            tlm_depth=function_tlm_depth(x) + 1)


def function_alias(x):
    return Function(x.function_space(), name=x.name(),
                    static=function_is_static(x), cache=function_is_cached(x),
                    checkpoint=function_is_checkpointed(x),
                    tlm_depth=function_tlm_depth(x), val=x.dat)


def function_zero(x):
    x.vector().dat.zero()


def function_global_size(x):
    return x.function_space().dim()


def function_local_indices(x):
    return slice(*x.vector().local_range())


def subtract_adjoint_derivative_action(x, y):
    if y is None:
        pass
    elif isinstance(y, tuple):
        alpha, y = y
        if isinstance(y, backend_Function):
            y = y.vector()
        # Works even if x or y are Vector objects
        function_axpy(x, -alpha, y)
    elif isinstance(y, ufl.classes.Form):
        if hasattr(x, "_tlm_adjoint__adj_b"):
            x._tlm_adjoint__adj_b += form_neg(y)
        else:
            x._tlm_adjoint__adj_b = form_neg(y)
    else:
        if isinstance(y, backend_Function):
            y = y.vector()
        # Works even if x or y are Vector objects
        function_axpy(x, -1.0, y)


def finalize_adjoint_derivative_action(x):
    if hasattr(x, "_tlm_adjoint__adj_b"):
        function_axpy(x, 1.0, backend_assemble(x._tlm_adjoint__adj_b))
        delattr(x, "_tlm_adjoint__adj_b")
