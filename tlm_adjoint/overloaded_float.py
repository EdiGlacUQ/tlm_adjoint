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

from .interface import DEFAULT_COMM, FunctionInterface, SpaceInterface, \
    add_interface, add_subtract_adjoint_derivative_action, check_space_type, \
    check_space_types, comm_dup_cached, function_assign, function_axpy, \
    function_comm, function_dtype, function_id, function_is_scalar, \
    function_name, function_new, function_new_conjugate_dual, \
    function_scalar_value, is_function, new_function_id, new_space_id, \
    space_comm, space_dtype

from .caches import Caches
from .equations import Assignment, Equation, ZeroAssignment, get_tangent_linear
from .manager import annotation_enabled, tlm_enabled

from collections.abc import Sequence
import contextlib
import functools
import numpy as np
import sympy as sp
from sympy.utilities.lambdify import lambdastr
try:
    from sympy.printing.numpy import NumPyPrinter
except ImportError:
    from sympy.printing.pycode import NumPyPrinter


__all__ = \
    [
        "FloatSpace",

        "OverloadedFloat",
        "SymbolicFloat",

        "Float",
        "FloatEquation",

        "no_float_overloading",
        "paused_float_overloading"
    ]


_name_counter = 0


def new_symbol_name():
    global _name_counter
    count = _name_counter
    _name_counter += 1
    return f"_tlm_adjoint_symbol__{count:d}"


_default_dtype = np.complex128


class FloatSpaceInterface(SpaceInterface):
    def _comm(self):
        return self._tlm_adjoint__space_interface_attrs["comm"]

    def _dtype(self):
        return self._tlm_adjoint__space_interface_attrs["dtype"]

    def _id(self):
        return self._tlm_adjoint__space_interface_attrs["id"]

    def _new(self, *, name=None, space_type="primal", static=False, cache=None,
             checkpoint=None):
        float_cls = self._tlm_adjoint__space_interface_attrs["float_cls"]
        return float_cls(
            name=name, space_type=space_type,
            static=static, cache=cache, checkpoint=checkpoint,
            dtype=space_dtype(self), comm=space_comm(self))


class FloatSpace:
    def __init__(self, float_cls=None, *, dtype=None, comm=None):
        if float_cls is None:
            float_cls = SymbolicFloat
        if dtype is None:
            dtype = _default_dtype
        if comm is None:
            comm = DEFAULT_COMM

        add_interface(self, FloatSpaceInterface,
                      {"comm": comm_dup_cached(comm),
                       "dtype": dtype, "float_cls": float_cls,
                       "id": new_space_id()})


_overload = 0


def no_float_overloading(fn):
    @functools.wraps(fn)
    def wrapped_fn(*args, **kwargs):
        with paused_float_overloading():
            return fn(*args, **kwargs)
    return wrapped_fn


@contextlib.contextmanager
def paused_float_overloading():
    global _overload
    _overload += 1
    try:
        yield
    finally:
        _overload -= 1


class FloatInterface(FunctionInterface):
    def _space(self):
        return self._tlm_adjoint__function_interface_attrs["space"]

    def _space_type(self):
        return self._tlm_adjoint__function_interface_attrs["space_type"]

    def _id(self):
        return self._tlm_adjoint__function_interface_attrs["id"]

    def _name(self):
        return self._tlm_adjoint__function_interface_attrs["name"]

    def _state(self):
        return self._tlm_adjoint__function_interface_attrs["state"]

    def _update_state(self):
        state = self._tlm_adjoint__function_interface_attrs["state"]
        self._tlm_adjoint__function_interface_attrs.d_setitem("state", state + 1)  # noqa: E501

    def _is_static(self):
        return self._tlm_adjoint__function_interface_attrs["static"]

    def _is_cached(self):
        return self._tlm_adjoint__function_interface_attrs["cache"]

    def _is_checkpointed(self):
        return self._tlm_adjoint__function_interface_attrs["checkpoint"]

    def _caches(self):
        return self._tlm_adjoint__function_interface_attrs["caches"]

    def _zero(self):
        function_assign(self, 0.0)

    def _assign(self, y):
        dtype = function_dtype(self)
        rdtype = type(dtype().real)

        if isinstance(y, SymbolicFloat):
            y = y.value()
        elif isinstance(y, (sp.Integer, sp.Float)):
            y = complex(y)
        if isinstance(y, (int, np.integer,
                          float, np.floating)):
            if not np.can_cast(y, rdtype):
                raise ValueError("Invalid dtype")
            self._value = rdtype(y)
        elif isinstance(y, (complex, np.complexfloating)):
            if y.imag == 0.0:
                if not np.can_cast(y.real, rdtype):
                    raise ValueError("Invalid dtype")
                self._value = rdtype(y.real)
            else:
                if not np.can_cast(y, dtype):
                    raise ValueError("Invalid dtype")
                self._value = dtype(y)
        else:
            function_assign(self, function_scalar_value(y))

    def _axpy(self, alpha, x, /):
        function_assign(self, self.value() + alpha * function_scalar_value(x))

    def _inner(self, y):
        return function_scalar_value(y).conjugate() * self.value()

    def _sum(self):
        return self.value()

    def _linf_norm(self):
        return abs(self.value())

    def _local_size(self):
        comm = function_comm(self)
        if comm.rank == 0:
            return 1
        else:
            return 0

    def _global_size(self):
        return 1

    def _local_indices(self):
        comm = function_comm(self)
        if comm.rank == 0:
            return slice(0, 1)
        else:
            return slice(0, 0)

    def _get_values(self):
        comm = function_comm(self)
        value = self.value()
        values = np.array([value] if comm.rank == 0 else [],
                          dtype=type(value))
        values.setflags(write=False)
        return values

    def _set_values(self, values):
        comm = function_comm(self)
        if comm.rank == 0:
            if values.shape != (1,):
                raise ValueError("Invalid shape")
            value, = values
        else:
            if values.shape != (0,):
                raise ValueError("Invalid shape")
            value = None
        value = comm.bcast(value, root=0)
        function_assign(self, value)

    def _replacement(self):
        return self

    def _is_replacement(self):
        return False

    def _is_scalar(self):
        return True

    def _scalar_value(self):
        # assert function_is_scalar(self)
        return self.value()


@no_float_overloading
def expr_dependencies(expr):
    deps = []
    for dep in expr.free_symbols:
        if isinstance(dep, SymbolicFloat):
            deps.append(dep)
        elif is_function(dep):
            raise ValueError("Invalid dependency")
    return sorted(deps, key=lambda dep: function_id(dep))


# Float class name already used by SymPy
class _tlm_adjoint__SymbolicFloat(sp.Symbol):  # noqa: N801
    def __init__(self, value=0.0, *, name=None, space_type="primal",
                 static=False, cache=None, checkpoint=None,
                 dtype=None, comm=None,
                 manager=None, annotate=None, tlm=None):
        id = new_function_id()
        if name is None:
            # Following FEniCS 2019.1.0 behaviour
            name = f"f_{id:d}"
        if space_type not in ["primal", "conjugate", "dual", "conjugate_dual"]:
            raise ValueError("Invalid space type")
        if cache is None:
            cache = static
        if checkpoint is None:
            checkpoint = not static

        super().__init__()
        self._value = 0.0
        add_interface(self, FloatInterface,
                      {"cache": cache, "checkpoint": checkpoint, "id": id,
                       "name": name, "state": 0,
                       "space": FloatSpace(type(self), dtype=dtype, comm=comm),
                       "space_type": space_type, "static": static})
        self._tlm_adjoint__function_interface_attrs["caches"] = Caches(self)

        if isinstance(value, (int, np.integer, sp.Integer,
                              float, np.floating, sp.Float,
                              complex, np.complexfloating)):
            if value != 0.0:
                function_assign(self, value)
        else:
            self.assign(value, manager=manager, annotate=annotate, tlm=tlm)

    def __new__(cls, value=0.0, *, name=None, space_type="primal",
                static=False, cache=None, checkpoint=None,
                dtype=None, comm=None,
                manager=None, annotate=None, tlm=None):
        return super().__new__(cls, new_symbol_name())

    def new(self, value=0.0, *,
            name=None,
            static=False, cache=None, checkpoint=None,
            manager=None, annotate=None, tlm=None):
        x = function_new(
            self, name=name,
            static=static, cache=cache, checkpoint=checkpoint)
        if isinstance(value, (int, np.integer, sp.Integer,
                              float, np.floating, sp.Float,
                              complex, np.complexfloating)):
            if value != 0.0:
                function_assign(x, value)
        else:
            x.assign(
                value,
                manager=manager, annotate=annotate, tlm=tlm)
        return x

    def __float__(self):
        return float(self.value())

    def __complex__(self):
        return complex(self.value())

    def assign(self, y, *, manager=None, annotate=None, tlm=None):
        if annotate is None:
            annotate = annotation_enabled(manager=manager)
        if tlm is None:
            tlm = tlm_enabled(manager=manager)
        if annotate or tlm:
            if isinstance(y, (int, np.integer, sp.Integer,
                              float, np.floating, sp.Float,
                              complex, np.complexfloating)):
                Assignment(self, self.new(y)).solve(
                    manager=manager, annotate=annotate, tlm=tlm)
            elif isinstance(y, sp.Expr):
                FloatEquation(self, y).solve(
                    manager=manager, annotate=annotate, tlm=tlm)
            else:
                raise TypeError(f"Unexpected type: {type(y)}")
        else:
            if isinstance(y, (int, np.integer, sp.Integer,
                              float, np.floating, sp.Float,
                              complex, np.complexfloating)):
                function_assign(self, y)
            elif isinstance(y, sp.Expr):
                deps = expr_dependencies(y)
                function_assign(
                    self,
                    lambdify(y, deps)(*(dep.value() for dep in deps)))
            else:
                raise TypeError(f"Unexpected type: {type(y)}")

    @no_float_overloading
    def addto(self, y, *, manager=None, annotate=None, tlm=None):
        x = self.new(value=self, name=f"{function_name(self):s}_old",
                     manager=manager, annotate=annotate, tlm=tlm)
        self.assign(x + y, manager=manager, annotate=annotate, tlm=tlm)

    def value(self):
        return self._value


SymbolicFloat = _tlm_adjoint__SymbolicFloat


class _tlm_adjoint__OverloadedFloat(SymbolicFloat):  # noqa: N801
    def __neg__(self):
        with paused_float_overloading():
            result = super().__neg__()
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __add__(self, other):
        with paused_float_overloading():
            result = super().__add__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __radd__(self, other):
        with paused_float_overloading():
            result = super().__radd__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __sub__(self, other):
        with paused_float_overloading():
            result = super().__sub__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __rsub__(self, other):
        with paused_float_overloading():
            result = super().__rsub__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __mul__(self, other):
        with paused_float_overloading():
            result = super().__mul__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __rmul__(self, other):
        with paused_float_overloading():
            result = super().__rmul__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __truediv__(self, other):
        with paused_float_overloading():
            result = super().__truediv__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __rtruediv__(self, other):
        with paused_float_overloading():
            result = super().__rtruediv__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __pow__(self, other):
        with paused_float_overloading():
            result = super().__pow__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    def __rpow__(self, other):
        with paused_float_overloading():
            result = super().__rpow__(other)
        if _overload == 0:
            return self.new(result)
        else:
            return result

    @no_float_overloading
    def sin(self):
        return self.new(sp.sin(self))

    @no_float_overloading
    def cos(self):
        return self.new(sp.cos(self))

    @no_float_overloading
    def tan(self):
        return self.new(sp.tan(self))

    @no_float_overloading
    def arcsin(self):
        return self.new(sp.arcsin(self))

    @no_float_overloading
    def arccos(self):
        return self.new(sp.arccos(self))

    @no_float_overloading
    def arctan(self):
        return self.new(sp.arctan(self))

    @no_float_overloading
    def arctan2(self, other):
        return self.new(sp.atan2(self, other))

    @no_float_overloading
    def sinh(self):
        return self.new(sp.sinh(self))

    @no_float_overloading
    def cosh(self):
        return self.new(sp.cosh(self))

    @no_float_overloading
    def tanh(self):
        return self.new(sp.tanh(self))

    @no_float_overloading
    def arcsinh(self):
        return self.new(sp.arcsinh(self))

    @no_float_overloading
    def arccosh(self):
        return self.new(sp.arccosh(self))

    @no_float_overloading
    def arctanh(self):
        return self.new(sp.arctanh(self))

    @no_float_overloading
    def exp(self):
        return self.new(sp.exp(self))

    @no_float_overloading
    def expm1(self):
        return self.new(sp.exp(self) - 1)

    @no_float_overloading
    def log(self):
        return self.new(sp.log(self))

    @no_float_overloading
    def log10(self):
        return self.new(sp.log(self, 10))

    @no_float_overloading
    def sqrt(self):
        return self.new(sp.sqrt(self))


OverloadedFloat = _tlm_adjoint__OverloadedFloat
Float = OverloadedFloat

_x = sp.Symbol(new_symbol_name())
_F = sp.utilities.lambdify(_x, _x, modules=["numpy"])
global_vars = _F.__globals__
del _x, _F


@no_float_overloading
def lambdify(expr, deps):
    printer = NumPyPrinter(
        settings={"fully_qualified_modules": False})
    code = lambdastr(deps, expr, printer=printer)
    assert "\n" not in code
    local_vars = {}
    exec(f"_tlm_adjoint__F = {code:s}", dict(global_vars), local_vars)
    F = local_vars["_tlm_adjoint__F"]
    F._tlm_adjoint__code = code
    return F


class FloatEquation(Equation):
    @no_float_overloading
    def __init__(self, x, expr):
        check_space_type(x, "primal")
        deps = expr_dependencies(expr)
        for dep in deps:
            check_space_type(dep, "primal")
        if function_id(x) in {function_id(dep) for dep in deps}:
            raise ValueError("Invalid dependency")
        deps.insert(0, x)

        dF_expr = {}
        nl_deps = {}
        for dep_index, dep in enumerate(deps[1:], start=1):
            expr_diff = dF_expr[dep_index] = expr.diff(dep)
            for dep2 in expr_dependencies(expr_diff):
                nl_deps.setdefault(function_id(dep), dep)
                nl_deps.setdefault(function_id(dep2), dep2)
        nl_deps = sorted(nl_deps.values(), key=lambda dep: function_id(dep))
        dF = {}
        for dep_index, expr_diff in dF_expr.items():
            dF[dep_index] = lambdify(expr_diff, nl_deps)

        super().__init__(x, deps, nl_deps=nl_deps,
                         ic=False, adj_ic=False)
        self._F_expr = expr
        self._F = lambdify(expr, deps)
        self._dF_expr = dF_expr
        self._dF = dF

    def forward_solve(self, x, deps=None):
        if deps is None:
            deps = self.dependencies()
        dep_vals = tuple(dep.value() for dep in deps)
        x_val = self._F(*dep_vals)
        function_assign(x, x_val)

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        return b

    def subtract_adjoint_derivative_actions(self, adj_x, nl_deps, dep_Bs):
        deps = self.dependencies()
        nl_dep_vals = tuple(nl_dep.value() for nl_dep in nl_deps)
        for dep_index, dep_B in dep_Bs.items():
            dep = deps[dep_index]
            F = function_new_conjugate_dual(dep)
            F_val = (-self._dF[dep_index](*nl_dep_vals).conjugate()
                     * adj_x.value())
            function_assign(F, F_val)
            dep_B.sub(F)

    @no_float_overloading
    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()
        expr = 0
        deps = self.dependencies()
        for dep_index, dF_expr in self._dF_expr.items():
            tau_dep = get_tangent_linear(deps[dep_index], M, dM, tlm_map)
            if tau_dep is not None:
                expr += dF_expr * tau_dep
        if isinstance(expr, int) and expr == 0:
            return ZeroAssignment(tlm_map[x])
        else:
            return FloatEquation(tlm_map[x], expr)


def _subtract_adjoint_derivative_action(x, y):
    if isinstance(x, SymbolicFloat):
        if is_function(y) and function_is_scalar(y):
            check_space_types(x, y)
            function_axpy(x, -1.0, y)
        elif isinstance(y, Sequence) \
                and len(y) == 2 \
                and isinstance(y[0], (int, np.integer,
                                      float, np.floating,
                                      complex, np.complexfloating)) \
                and is_function(y[1]) and function_is_scalar(y[1]):
            check_space_types(x, y[1])
            function_axpy(x, -y[0], y[1])
        else:
            return NotImplemented
    else:
        return NotImplemented


add_subtract_adjoint_derivative_action(
    "_tlm_adjoint__SymbolicFloat", _subtract_adjoint_derivative_action)
