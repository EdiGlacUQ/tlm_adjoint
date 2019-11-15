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
from .interface import *
from .interface import FunctionInterface as _FunctionInterface

import copy
import mpi4py.MPI as MPI
import numpy as np
import ufl
import warnings
import weakref

__all__ = \
    [
        "Constant",
        "DirichletBC",
        "Function",
        "HomogeneousDirichletBC",
        "Replacement",
        "ZeroConstant",
        "ZeroFunction",
        "bcs_is_cached",
        "bcs_is_homogeneous",
        "bcs_is_static",
        "new_count"
    ]


def new_count():
    c = backend_Constant.__new__(backend_Constant, 0.0)
    _orig_Constant__init__(c, 0.0)
    return c.count()


class Caches:
    def __init__(self, x):
        self._caches = weakref.WeakValueDictionary()
        self._id = function_id(x)
        self._state = (self._id, function_state(x))

    def __len__(self):
        return len(self._caches)

    def clear(self):
        for cache in tuple(self._caches.valuerefs()):
            cache = cache()
            if cache is not None:
                cache.clear(self._id)
                assert not cache.id() in self._caches

    def add(self, cache):
        cache_id = cache.id()
        if cache_id not in self._caches:
            self._caches[cache_id] = cache

    def remove(self, cache):
        del self._caches[cache.id()]

    def update(self, x):
        state = (function_id(x), function_state(x))
        if state != self._state:
            self.clear()
            self._state = state


class ConstantSpaceInterface(SpaceInterface):
    def _comm(self):
        return self._tlm_adjoint__space_interface_attrs["comm"]

    def _id(self):
        return self._tlm_adjoint__space_interface_attrs["id"]

    def _new(self, name=None, static=False, cache=None, checkpoint=None):
        return Constant(space=self, name=name, static=static, cache=cache,
                        checkpoint=checkpoint)


class ConstantInterface(_FunctionInterface):
    def _comm(self):
        return space_comm(self._tlm_adjoint__function_interface_attrs["space"])

    def _space(self):
        return self._tlm_adjoint__function_interface_attrs["space"]

    def _id(self):
        return self.count()

    def _name(self):
        if hasattr(self, "name"):
            return self.name()
        else:
            # Following FEniCS 2019.1.0 behaviour
            return "f_{self.count():d}"

    def _state(self):
        if not hasattr(self, "_tlm_adjoint__state"):
            self._tlm_adjoint__state = 0
        return self._tlm_adjoint__state

    def _update_state(self):
        if hasattr(self, "_tlm_adjoint__state"):
            self._tlm_adjoint__state += 1
        else:
            self._tlm_adjoint__state = 1

    def _is_static(self):
        if hasattr(self, "is_static"):
            return self.is_static()
        else:
            return False

    def _is_cached(self):
        if hasattr(self, "is_cached"):
            return self.is_cached()
        else:
            return False

    def _is_checkpointed(self):
        if hasattr(self, "is_checkpointed"):
            return self.is_checkpointed()
        else:
            return True

    def _caches(self):
        if not hasattr(self, "_tlm_adjoint__caches"):
            self._tlm_adjoint__caches = Caches(self)
        return self._tlm_adjoint__caches

    def _zero(self):
        if len(self.ufl_shape) == 0:
            value = 0.0
        else:
            value = np.zeros(self.ufl_shape, dtype=np.float64)
            value = backend_Constant(value)
        self.assign(value)  # annotate=False, tlm=False

    def _assign(self, y):
        self.assign(y)  # annotate=False, tlm=False

    def _axpy(self, alpha, y):
        if len(self.ufl_shape) == 0:
            value = float(self) + alpha * float(y)
        else:
            value = self.values() + alpha * y.values()
            value = backend_Constant(value)
        self.assign(value)  # annotate=False, tlm=False

    def _inner(self, y):
        return (self.values() * y.values()).sum()

    def _max_value(self):
        return self.values().max()

    def _sum(self):
        return self.values().sum()

    def _linf_norm(self):
        return abs(self.values()).max()

    def _local_size(self):
        comm = function_comm(self)
        if comm.rank == 0:
            if len(self.ufl_shape) == 0:
                return 1
            else:
                return np.prod(self.ufl_shape)
        else:
            return 0

    def _global_size(self):
        if len(self.ufl_shape) == 0:
            return 1
        else:
            return np.prod(self.ufl_shape)

    def _local_indices(self):
        comm = function_comm(self)
        if comm.rank == 0:
            if len(self.ufl_shape) == 0:
                return slice(0, 1)
            else:
                return slice(0, np.prod(self.ufl_shape))
        else:
            return slice(0, 0)

    def _get_values(self):
        comm = function_comm(self)
        if comm.rank == 0:
            values = self.values().view()
        else:
            values = np.array([], dtype=np.float64)
        values.setflags(write=False)
        return values

    def _set_values(self, values):
        comm = function_comm(self)
        if comm.rank != 0:
            if len(self.ufl_shape) == 0:
                values = np.array([0.0], dtype=np.float64)
            else:
                values = np.zeros(self.ufl_shape, dtype=np.float64)
        values = comm.bcast(values, root=0)
        if len(self.ufl_shape) == 0:
            self.assign(values[0])  # annotate=False, tlm=False
        else:
            self.assign(backend_Constant(values))  # annotate=False, tlm=False

    def _new(self, name=None, static=False, cache=None, checkpoint=None):
        space = self._tlm_adjoint__function_interface_attrs["space"]
        comm = function_comm(self)
        return Constant(space=space, comm=comm, name=name, static=static,
                        cache=cache, checkpoint=checkpoint)

    def _copy(self, name=None, static=False, cache=None, checkpoint=None):
        if len(self.ufl_shape) == 0:
            value = float(self)
        else:
            value = self.values()
        space = self._tlm_adjoint__function_interface_attrs["space"]
        comm = function_comm(self)
        return Constant(value, space=space, comm=comm, name=name,
                        static=static, cache=cache, checkpoint=checkpoint)

    def _tangent_linear(self, name=None):
        if hasattr(self, "tangent_linear"):
            return self.tangent_linear(name=name)
        elif function_is_static(self):
            return None
        else:
            space = self._tlm_adjoint__function_interface_attrs["space"]
            comm = function_comm(self)
            return Constant(space=space, comm=comm, name=name, static=False,
                            cache=function_is_cached(self),
                            checkpoint=function_is_checkpointed(self))

    def _replacement(self):
        if not hasattr(self, "_tlm_adjoint__replacement"):
            # Firedrake requires Constant.function_space() to return None
            self._tlm_adjoint__replacement = \
                Replacement(self, space=None)
        return self._tlm_adjoint__replacement


_orig_Constant__init__ = backend_Constant.__init__


def _Constant__init__(self, *args, space=None, comm=MPI.COMM_WORLD, **kwargs):
    _orig_Constant__init__(self, *args, **kwargs)

    if space is None:
        space = self.ufl_function_space()
        add_interface(space, ConstantSpaceInterface,
                      {"comm": comm, "id": new_count()})
    add_interface(self, ConstantInterface,
                  {"space": space})


backend_Constant.__init__ = _Constant__init__


class Constant(backend_Constant):
    def __init__(self, value=None, *args, space=None, shape=None, comm=None,
                 static=False, cache=None, checkpoint=None, **kwargs):
        # Shape initialization / checking
        if space is not None:
            if shape is None:
                shape = space.ufl_element().value_shape()
            elif shape != space.ufl_element().value_shape():
                raise InterfaceException("Invalid shape")
        if value is None:
            if shape is None:
                shape = tuple()
        elif shape is not None:
            value_ = value
            if not isinstance(value_, np.ndarray):
                value_ = np.array(value_)
            if value_.shape != shape:
                raise InterfaceException("Invalid shape")
            del value_

        # Default value
        if value is None:
            if len(shape) == 0:
                value = 0.0
            else:
                value = np.zeros(shape, dtype=np.float64)

        # Default comm
        if comm is None:
            if space is None:
                comm = MPI.COMM_WORLD
            else:
                comm = space_comm(space)

        if cache is None:
            cache = static
        if checkpoint is None:
            checkpoint = not static

        # "name" constructor argument not supported by Firedrake
        if not hasattr(backend_Constant, "name"):
            kwargs = copy.copy(kwargs)
            name = kwargs.pop("name", None)

        backend_Constant.__init__(self, value, *args, space=space, comm=comm,
                                  **kwargs)
        self.__static = static
        self.__cache = cache
        self.__checkpoint = checkpoint

        if not hasattr(backend_Constant, "name"):
            if name is None:
                # Following FEniCS 2019.1.0 behaviour
                name = f"f_{self.count():d}"
            self.name = lambda: name

    def is_static(self):
        return self.__static

    def is_cached(self):
        return self.__cache

    def is_checkpointed(self):
        return self.__checkpoint

    def tangent_linear(self, name=None):
        if self.is_static():
            return None
        else:
            return Constant(space=function_space(self),
                            comm=function_comm(self), name=name, static=False,
                            cache=self.is_cached(),
                            checkpoint=self.is_checkpointed())

    def ufl_domain(self):
        return function_space(self).ufl_domain()

    def ufl_domains(self):
        return function_space(self).ufl_domains()


class Zero(Constant):
    def __init__(self, space=None, shape=None, name=None, comm=None):
        Constant.__init__(self, space=space, shape=shape, name=name, comm=comm,
                          static=True)

    def assign(self, *args, **kwargs):
        raise InterfaceException("Cannot call assign method of Zero")

    def _tlm_adjoint__function_interface_assign(self, y):
        raise InterfaceException("Cannot call _assign interface of Zero")

    def _tlm_adjoint__function_interface_axpy(self, alpha, y):
        raise InterfaceException("Cannot call _axpy interface of Zero")

    def _tlm_adjoint__function_interface_set_values(self, values):
        raise InterfaceException("Cannot call _set_values interface of Zero")


class ZeroConstant(Zero):
    def __init__(self, shape=None, name=None, comm=MPI.COMM_WORLD):
        Zero.__init__(self, shape=shape, name=name, comm=comm)


class ZeroFunction(Zero):
    def __init__(self, space, name=None):
        Zero.__init__(self, space=space, name=name)


class Function(backend_Function):
    def __init__(self, *args, static=False, cache=None, checkpoint=None,
                 **kwargs):
        if cache is None:
            cache = static
        if checkpoint is None:
            checkpoint = not static

        self.__static = static
        self.__cache = cache
        self.__checkpoint = checkpoint
        backend_Function.__init__(self, *args, **kwargs)

    def is_static(self):
        return self.__static

    def is_cached(self):
        return self.__cache

    def is_checkpointed(self):
        return self.__checkpoint

    def tangent_linear(self, name=None):
        if self.is_static():
            return None
        else:
            return function_new(self, name=name, static=False,
                                cache=self.is_cached(),
                                checkpoint=self.is_checkpointed())


class DirichletBC(backend_DirichletBC):
    # Based on FEniCS 2019.1.0 DirichletBC API
    def __init__(self, V, g, sub_domain, *args, static=None, cache=None,
                 homogeneous=None, _homogeneous=None, **kwargs):
        backend_DirichletBC.__init__(self, V, g, sub_domain, *args, **kwargs)

        if static is None:
            static = True
            for dep in ufl.algorithms.extract_coefficients(
                    g if isinstance(g, ufl.classes.Expr)
                    else backend_Constant(g)):
                # The 'static' flag for functions is only a hint. 'not
                # checkpointed' is a guarantee that the function will never
                # appear as the solution to an Equation.
                if not is_function(dep) or not function_is_checkpointed(dep):
                    static = False
                    break
        if cache is None:
            cache = static
        if homogeneous is not None:
            warnings.warn("homogeneous argument is deprecated -- "
                          "use HomogeneousDirichletBC instead",
                          DeprecationWarning, stacklevel=2)
            if _homogeneous is not None:
                raise InterfaceException("Cannot supply both homogeneous and "
                                         "_homogeneous arguments")
        elif _homogeneous is None:
            homogeneous = False
        else:
            homogeneous = _homogeneous

        self.__static = static
        self.__cache = cache
        self.__homogeneous = homogeneous

    def is_static(self):
        return self.__static

    def is_cached(self):
        return self.__cache

    def is_homogeneous(self):
        return self.__homogeneous

    def homogenize(self):
        if self.is_static():
            raise InterfaceException("Cannot call homogenize method for "
                                     "static DirichletBC")
        if not self.__homogeneous:
            backend_DirichletBC.homogenize(self)
            self.__homogeneous = True

    def set_value(self, *args, **kwargs):
        if self.is_static():
            raise InterfaceException("Cannot call set_value method for "
                                     "static DirichletBC")
        backend_DirichletBC.set_value(self, *args, **kwargs)


class HomogeneousDirichletBC(DirichletBC):
    # Based on FEniCS 2019.1.0 DirichletBC API
    def __init__(self, V, sub_domain, *args, **kwargs):
        shape = V.ufl_element().value_shape()
        if len(shape) == 0:
            g = backend_Constant(0.0)
        else:
            g = backend_Constant(np.zeros(shape, dtype=np.float64))
        DirichletBC.__init__(self, V, g, sub_domain, *args, static=True,
                             _homogeneous=True, **kwargs)


def bcs_is_static(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_static") or not bc.is_static():
            return False
    return True


def bcs_is_cached(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_cached") or not bc.is_cached():
            return False
    return True


def bcs_is_homogeneous(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_homogeneous") or not bc.is_homogeneous():
            return False
    return True


class ReplacementInterface(_FunctionInterface):
    def _space(self):
        return self.ufl_function_space()

    def _id(self):
        return self.id()

    def _name(self):
        return self.name()

    def _state(self):
        return -1

    def _is_static(self):
        return self.is_static()

    def _is_cached(self):
        return self.is_cached()

    def _is_checkpointed(self):
        return self.is_checkpointed()

    def _caches(self):
        return self.caches()

    def _new(self, name=None, static=False, cache=None, checkpoint=None):
        return space_new(self._tlm_adjoint__function_interface_attrs["space"],
                         name=name, static=static, cache=cache,
                         checkpoint=checkpoint)

    def _replacement(self):
        return self


class Replacement(ufl.classes.Coefficient):
    def __init__(self, x, *args, **kwargs):
        if len(args) > 0 or len(kwargs) > 0:
            def extract_args(x, space):
                return x, space
            x, space = extract_args(x, *args, **kwargs)
            x_space = function_space(x)
        else:
            space = function_space(x)
            x_space = space

        ufl.classes.Coefficient.__init__(self, x_space, count=new_count())
        self.__space = space
        self.__id = function_id(x)
        self.__name = function_name(x)
        self.__static = function_is_static(x)
        self.__cache = function_is_cached(x)
        self.__checkpoint = function_is_checkpointed(x)
        self.__caches = function_caches(x)
        add_interface(self, ReplacementInterface, {"space": x_space})

    def function_space(self):
        return self.__space

    def id(self):
        return self.__id

    def name(self):
        return self.__name

    def is_static(self):
        return self.__static

    def is_cached(self):
        return self.__cache

    def is_checkpointed(self):
        return self.__checkpoint

    def caches(self):
        return self.__caches
