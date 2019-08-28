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

from .backend_interface import *

from .base_equations import AdjointModelRHS, ControlsMarker, EquationAlias, \
    FunctionalMarker, NullSolver
from .binomial_checkpointing import MultistageManager
from .manager import manager as _manager, set_manager

from collections import OrderedDict, defaultdict, deque
import copy
import numpy as np
import pickle
import os
import weakref
import zlib

__all__ = \
    [
        "CheckpointStorage",
        "Control",
        "EquationManager",
        "ManagerException",
        "add_tlm",
        "annotation_enabled",
        "compute_gradient",
        "configure_checkpointing",
        "manager_info",
        "minimize_scipy",
        "new_block",
        "reset",
        "reset_manager",
        "start_annotating",
        "start_manager",
        "start_tlm",
        "stop_annotating",
        "stop_manager",
        "stop_tlm",
        "taylor_test",
        "taylor_test_tlm",
        "taylor_test_tlm_adjoint",
        "tlm",
        "tlm_enabled"
    ]


class ManagerException(Exception):
    pass


class Control:
    def __init__(self, m, manager=None):
        if manager is None:
            manager = _manager()

        if isinstance(m, str):
            m = manager.find_initial_condition(m)

        self._m = m

    def m(self):
        return self._m


class CheckpointStorage:
    def __init__(self, store_ics=True, store_data=True):
        self._seen_ics = set()
        self._cp = {}
        self._indices = defaultdict(lambda: 0)
        self._dep_keys = {}
        self._data = {}
        self._refs = {}

        self.configure(store_ics=store_ics,
                       store_data=store_data)

    def configure(self, store_ics=None, store_data=None):
        """
        Configure storage.

        Arguments:

        store_ics   Store initial condition data, used by checkpointing
        store_data  Store equation non-linear dependency data, used in reverse
                    mode
        """

        if store_ics is not None:
            self._store_ics = store_ics
        if store_data is not None:
            self._store_data = store_data

    def store_ics(self):
        return self._store_ics

    def store_data(self):
        return self._store_data

    def clear(self, clear_cp=True, clear_data=True, clear_refs=False):
        if clear_cp:
            self._seen_ics.clear()
            self._cp.clear()
        if clear_data:
            self._indices.clear()
            self._dep_keys.clear()
            self._data.clear()
        if clear_refs:
            self._refs.clear()
        else:
            for x_id, x in self._refs.items():
                # May have been cleared above
                self._seen_ics.add(x_id)

                x_key = self._data_key(x_id)
                if x_key not in self._data:
                    self._data[x_key] = x

    def __getitem__(self, key):
        return tuple(self._data[dep_key] for dep_key in self._dep_keys[key])

    def initial_condition(self, x, copy=True):
        x_id = x.id()
        if x_id in self._refs:
            ic = self._refs[x_id]
        else:
            ic = self._cp[x_id]
        if copy:
            ic = function_copy(ic)
        return ic

    def initial_conditions(self, cp=True, refs=False, copy=True):
        cp_d = {}
        if cp:
            for x_id, x in self._cp.items():
                cp_d[x_id] = function_copy(x) if copy else x
        if refs:
            for x_id, x in self._refs.items():
                cp_d[x_id] = function_copy(x) if copy else x
        return cp_d

    def _data_key(self, x_id):
        return (x_id, self._indices[x_id])

    def add_initial_condition(self, x, value=None, copy=None):
        if value is None:
            value = x
        if copy is None:
            copy = function_is_checkpointed(x)
        self._add_initial_condition(x_id=x.id(), value=value, copy=copy)

    def _add_initial_condition(self, x_id, value, copy):
        if self._store_ics and x_id not in self._seen_ics:
            x_key = self._data_key(x_id)
            if x_key in self._data:
                if copy:
                    self._cp[x_id] = self._data[x_key]
                else:
                    assert(x_id in self._refs)
            else:
                if copy:
                    value = function_copy(value)
                    self._cp[x_id] = value
                else:
                    self._refs[x_id] = value
                self._data[x_key] = value
            self._seen_ics.add(x_id)

    def add_equation(self, key, eq, deps=None, nl_deps=None,
                     copy=lambda x: function_is_checkpointed(x)):
        eq_X = eq.X()
        eq_deps = eq.dependencies()
        if deps is None:
            deps = eq_deps

        for eq_x in eq_X:
            self._indices[eq_x.id()] += 1

        if self._store_ics:
            for eq_x in eq_X:
                self._seen_ics.add(eq_x.id())
            for eq_dep, dep in zip(eq_deps, deps):
                self.add_initial_condition(eq_dep, value=dep,
                                           copy=copy(eq_dep))

        if self._store_data:
            if nl_deps is None:
                nl_deps = tuple(deps[i]
                                for i in eq.nonlinear_dependencies_map())

            dep_keys = []
            for eq_dep, dep in zip(eq.nonlinear_dependencies(), nl_deps):
                eq_dep_id = eq_dep.id()
                dep_key = self._data_key(eq_dep_id)
                if dep_key not in self._data:
                    if copy(eq_dep):
                        self._data[dep_key] = function_copy(dep)
                    else:
                        self._data[dep_key] = dep
                        if eq_dep_id not in self._seen_ics:
                            self._seen_ics.add(eq_dep_id)
                            self._refs[eq_dep_id] = dep
                dep_keys.append(dep_key)
            self._dep_keys[key] = dep_keys


class TangentLinearMap:
    """
    A map from forward to tangent-linear variables.
    """

    def __init__(self, name_suffix=" (tangent-linear)"):
        self._name_suffix = name_suffix
        self._map = {}
        self._finalizes = {}

    def __del__(self):
        for finalize in self._finalizes.values():
            finalize.detach()

    def __contains__(self, x):
        return x.id() in self._map

    def __getitem__(self, x):
        if not is_function(x):
            raise ManagerException("x must be a Function")
        x_id = x.id()
        if x_id not in self._map:
            def callback(self_ref, x_id):
                self = self_ref()
                if self is not None:
                    del(self._map[x_id])
                    del(self._finalizes[x_id])
            self._finalizes[x_id] = weakref.finalize(
                x, callback, weakref.ref(self), x_id)
            self._map[x_id] = function_tangent_linear(
                x, name=f"{function_name(x):s}{self._name_suffix:s}")
        return self._map[x_id]


class ReplayStorage:
    def __init__(self, blocks, N0, N1):
        # Map from dep (id) to (indices of) last equation which depends on dep
        last_eq = {}
        for n in range(N0, N1):
            for i, eq in enumerate(blocks[n]):
                for dep in eq.dependencies():
                    last_eq[dep.id()] = (n, i)

        # Ordered container, with each element containing a set of dep ids for
        # which the corresponding equation is the last equation to depend on
        # dep
        eq_last_q = deque()
        eq_last_d = {}
        for n in range(N0, N1):
            for i in range(len(blocks[n])):
                dep_ids = set()
                eq_last_q.append(dep_ids)
                eq_last_d[(n, i)] = dep_ids
        for dep_id, (n, i) in last_eq.items():
            eq_last_d[(n, i)].add(dep_id)
        del(eq_last_d)

        self._eq_last = eq_last_q
        self._map = {dep_id: None for dep_id in last_eq.keys()}

    def __len__(self):
        return len(self._map)

    def __contains__(self, x):
        if isinstance(x, int):
            x_id = x
        else:
            x_id = x.id()
        return x_id in self._map

    def __getitem__(self, x):
        if isinstance(x, int):
            y = self._map[x]
            if y is None:
                raise ManagerException("Unable to create new Function")
        else:
            x_id = x.id()
            y = self._map[x_id]
            if y is None:
                y = self._map[x_id] = function_new(x)
        return y

    def __setitem__(self, x, y):
        if isinstance(x, int):
            x_id = x
        else:
            x_id = x.id()
        if x_id in self._map:
            self._map[x_id] = y
        return y

    def update(self, d, copy=True):
        for key, value in d.items():
            if key in self:
                self[key] = function_copy(value) if copy else value

    def pop(self):
        for dep_id in self._eq_last.popleft():
            del(self._map[dep_id])


class DependencyTransposer:
    def __init__(self, blocks, M):
        dep_map = {}
        eq_X_ids = []
        M_ids = set(m.id() for m in M)
        active_ids = set()
        for p, block in enumerate(blocks):
            for k, eq in enumerate(block):
                eq_active = False
                X_ids = set(x.id() for x in eq.X())
                for dep in eq.dependencies():
                    dep_id = dep.id()
                    if dep_id not in X_ids and dep_id in active_ids:
                        eq_active = True
                        break
                X_ids = tuple(x.id() for x in eq.X())
                if eq_active:
                    # A solution component depends on the control(s)
                    for m, x_id in enumerate(X_ids):
                        active_ids.add(x_id)
                        if x_id in dep_map:
                            dep_map[x_id].append((p, k, m))
                        else:
                            dep_map[x_id] = [(p, k, m)]
                    eq_X_ids.append(X_ids)
                else:
                    eq_X_active_ids = []
                    for m, x_id in enumerate(X_ids):
                        if x_id in M_ids:
                            # Control
                            M_ids.remove(x_id)
                            assert(x_id not in active_ids)
                            active_ids.add(x_id)
                            assert(x_id not in dep_map)
                            dep_map[x_id] = [(p, k, m)]
                            eq_X_active_ids.append(x_id)
                        elif x_id in active_ids:
                            active_ids.remove(x_id)
                    eq_X_ids.append(tuple(eq_X_active_ids))

        self._dep_map = dep_map
        self._eq_X_ids = eq_X_ids

    def __contains__(self, dep):
        if isinstance(dep, int):
            dep_id = dep
        else:
            dep_id = dep.id()
        return dep_id in self._dep_map

    def __getitem__(self, dep):
        if isinstance(dep, int):
            dep_id = dep
        else:
            dep_id = dep.id()
        return self._dep_map[dep_id][-1]

    def pop(self):
        for x_id in self._eq_X_ids.pop():
            self._dep_map[x_id].pop()
            if len(self._dep_map[x_id]) == 0:
                del(self._dep_map[x_id])

    def is_empty(self):
        return len(self._dep_map) == 0 and len(self._eq_X_ids) == 0


class EquationManager:
    _id_counter = [0]

    def __init__(self, comm=None, cp_method="memory", cp_parameters={}):
        """
        Manager for tangent-linear and adjoint models.

        Arguments:
        comm  (Optional) Communicator. Default default_comm().

        cp_method  (Optional) Checkpointing method. Default "memory".
            Possible methods
                memory
                    Store everything in RAM.
                periodic_disk
                    Periodically store initial condition data on disk.
                multistage
                    Binomial checkpointing using the approach described in
                        GW2000  A. Griewank and A. Walther, "Algorithm 799:
                                Revolve: An implementation of checkpointing for
                                the reverse or adjoint mode of computational
                                differentiation", ACM Transactions on
                                Mathematical Software, 26(1), pp. 19--45, 2000
                    with a brute force search used to obtain behaviour
                    described in
                        SW2009  P. Stumm and A. Walther, "MultiStage approaches
                                for optimal offline checkpointing", SIAM
                                Journal on Scientific Computing, 31(3),
                                pp. 1946--1967, 2009

        cp_parameters  (Optional) Checkpointing parameters dictionary.
            Parameters for "memory" method
                replace        Whether to automatically replace internal
                               Function objects in the provided equations with
                               ReplacementFunction objects. Logical, optional,
                               default False.

            Parameters for "periodic_disk" method
                path           Directory in which disk checkpoint data should
                               be stored. String, optional, default
                               "checkpoints~".
                format         Disk checkpointing format. One of {"pickle",
                               "hdf5"}, optional, default "hdf5".
                period         Interval between checkpoints. Positive integer,
                               required.

            Parameters for "multistage" method
                path           Directory in which disk checkpoint data should
                               be stored. String, optional, default
                               "checkpoints~".
                format         Disk checkpointing format. One of {"pickle",
                               "hdf5"}, optional, default "hdf5".
                blocks         Total number of blocks. Positive integer,
                               required.
                snaps_in_ram   Number of "snaps" to store in RAM. Non-negative
                               integer, optional, default 0.
                snaps_on_disk  Number of "snaps" to store on disk. Non-negative
                               integer, optional, default 0.
                verbose        Whether to enable increased verbosity. Logical,
                               optional, default False.
        """
        # "multistage" name, and "snaps_in_ram", "snaps_on_disk" and "verbose"
        # in "multistage" method, are similar to adj_checkpointing arguments in
        # dolfin-adjoint 2017.1.0

        if comm is None:
            comm = default_comm()
        # FEniCS backwards compatibility
        if hasattr(comm, "tompi4py"):
            comm = comm.tompi4py()

        self._comm = comm
        if self._comm.rank == 0:
            id = self._id_counter[0]
            self._id_counter[0] += 1
            comm_py2f = self._comm.py2f()
        else:
            id = -1
            comm_py2f = -1
        self._id = self._comm.bcast(id, root=0)
        self._comm_py2f = self._comm.bcast(comm_py2f, root=0)
        self.reset(cp_method=cp_method, cp_parameters=cp_parameters)

    def __del__(self):
        for finalize in self._finalizes.values():
            finalize.detach()

    def comm(self):
        return self._comm

    def info(self, info=info):
        """
        Display information about the equation manager state.

        Arguments:

        info  A callable which displays a provided string.
        """

        info("Equation manager status:")
        info(f"Annotation state: {self._annotation_state:s}")
        info(f"Tangent-linear state: {self._tlm_state:s}")
        info("Equations:")
        blocks = copy.copy(self._blocks)
        if len(self._block) > 0:
            blocks.append(self._block)
        for n, block in enumerate(blocks):
            info(f"  Block {n:d}")
            for i, eq in enumerate(block):
                eq_X = eq.X()
                if len(eq_X) == 1:
                    X_name = function_name(eq_X[0])
                    X_ids = f"id {eq_X[0].id():d}"
                else:
                    X_name = "(%s)" % (",".join(function_name(eq_x)
                                                for eq_x in eq_X))
                    X_ids = "ids (%s)" % (",".join(f"{eq_x.id():d}"
                                                   for eq_x in eq_X))
                if isinstance(eq, EquationAlias):
                    eq_type = f"{eq:s}"
                else:
                    eq_type = type(eq).__name__
                info("    Equation %i, %s solving for %s (%s)" %
                     (i, eq_type, X_name, X_ids))
                nl_dep_ids = set([dep.id()
                                 for dep in eq.nonlinear_dependencies()])
                for j, dep in enumerate(eq.dependencies()):
                    info("      Dependency %i, %s (id %i)%s, %s" %
                         (j, function_name(dep), dep.id(),
                         ", replaced" if isinstance(dep, ReplacementFunction) else "",  # noqa: E501
                         "non-linear" if dep.id() in nl_dep_ids else "linear"))
        info("Storage:")
        info(f'  Storing initial conditions: {"yes" if self._cp.store_ics() else "no":s}')  # noqa: E501
        info(f'  Storing equation non-linear dependencies: {"yes" if self._cp.store_data() else "no":s}')  # noqa: E501
        info(f"  Initial conditions stored: {len(self._cp._cp):d}")
        info(f"  Initial conditions referenced: {len(self._cp._refs):d}")
        info(f"  Equations with non-linear dependencies: {len(self._cp._dep_keys):d}")  # noqa: E501
        info("Checkpointing:")
        info(f"  Method: {self._cp_method:s}")
        if self._cp_method == "memory":
            pass
        elif self._cp_method == "periodic_disk":
            info(f"  Function spaces referenced: {len(self._cp_spaces):d}")
        elif self._cp_method == "multistage":
            info(f"  Function spaces referenced: {len(self._cp_spaces):d}")
            info(f"  Snapshots in RAM: {self._cp_manager.snapshots_in_ram():d}")  # noqa: E501
            info(f"  Snapshots on disk: {self._cp_manager.snapshots_on_disk():d}")  # noqa: E501
        else:
            raise ManagerException(f"Unrecognized checkpointing method: {self._cp_method:s}")  # noqa: E501

    def new(self, cp_method=None, cp_parameters=None):
        """
        Return a new equation manager sharing the communicator of this
        equation manager. Optionally a new checkpointing configuration can be
        provided.
        """

        if cp_method is None:
            cp_method = self._cp_method
        if cp_parameters is None:
            cp_parameters = self._cp_parameters

        return EquationManager(comm=self._comm, cp_method=cp_method,
                               cp_parameters=cp_parameters)

    def reset(self, cp_method=None, cp_parameters=None):
        """
        Reset the equation manager. Optionally a new checkpointing
        configuration can be provided.
        """

        if cp_method is None:
            cp_method = self._cp_method
        if cp_parameters is None:
            cp_parameters = self._cp_parameters

        self._annotation_state = "initial"
        self._tlm_state = "initial"
        self._blocks = []
        self._block = []
        self._replaced = set()
        self._replace_map = {}
        if hasattr(self, "_finalizes"):
            for finalize in self._finalizes.values():
                finalize.detach()
        self._finalizes = {}

        self._tlm = OrderedDict()
        self._tlm_eqs = {}

        self.configure_checkpointing(cp_method, cp_parameters=cp_parameters)

    def configure_checkpointing(self, cp_method, cp_parameters={}):
        """
        Provide a new checkpointing configuration.
        """

        if self._annotation_state not in ["initial", "stopped_initial"]:
            raise ManagerException("Cannot configure checkpointing after annotation has started, or after finalization")  # noqa: E501

        cp_parameters = copy_parameters_dict(cp_parameters)

        if cp_method == "memory":
            disk_storage = False
        elif cp_method == "periodic_disk":
            disk_storage = True
        elif cp_method == "multistage":
            disk_storage = cp_parameters.get("snaps_on_disk", 0) > 0
        else:
            raise ManagerException(f"Unrecognized checkpointing method: {cp_method:s}")  # noqa: E501

        if disk_storage:
            cp_parameters["path"] = cp_path = cp_parameters.get("path", "checkpoints~")  # noqa: E501
            cp_parameters["format"] = cp_parameters.get("format", "hdf5")

            if self._comm.rank == 0:
                if not os.path.exists(cp_path):
                    os.makedirs(cp_path)
            self._comm.barrier()

        if cp_method == "memory":
            cp_manager = None
            cp_parameters["replace"] = cp_parameters.get("replace", False)
        elif cp_method == "periodic_disk":
            cp_manager = set()
        elif cp_method == "multistage":
            cp_blocks = cp_parameters["blocks"]
            cp_parameters["snaps_in_ram"] = cp_snaps_in_ram = cp_parameters.get("snaps_in_ram", 0)  # noqa: E501
            cp_parameters["snaps_on_disk"] = cp_snaps_on_disk = cp_parameters.get("snaps_on_disk", 0)  # noqa: E501
            cp_parameters["verbose"] = cp_parameters.get("verbose", False)

            cp_manager = MultistageManager(cp_blocks,
                                           cp_snaps_in_ram, cp_snaps_on_disk)
        else:
            raise ManagerException(f"Unrecognized checkpointing method: {cp_method:s}")  # noqa: E501

        self._cp_method = cp_method
        self._cp_parameters = cp_parameters
        self._cp_manager = cp_manager
        self._cp_spaces = {}
        self._cp_memory = {}

        if cp_method == "multistage":
            def debug_info(message):
                if self._cp_parameters["verbose"]:
                    info(message)

            if self._cp_manager.max_n() == 1:
                debug_info("forward: configuring storage for reverse")
                self._cp = CheckpointStorage(store_ics=True,
                                             store_data=True)
            else:
                debug_info("forward: configuring storage for snapshot")
                self._cp = CheckpointStorage(store_ics=True,
                                             store_data=False)
                debug_info(f"forward: deferred snapshot at {self._cp_manager.n():d}")  # noqa: E501
                self._cp_manager.snapshot()
            self._cp_manager.forward()
            debug_info(f"forward: forward advance to {self._cp_manager.n():d}")
        else:
            self._cp = CheckpointStorage(store_ics=True,
                                         store_data=cp_method == "memory")

    def add_tlm(self, M, dM, max_depth=1):
        """
        Add a tangent-linear model computing derivatives with respect to the
        control defined by M in the direction defined by dM.
        """

        if self._tlm_state == "final":
            raise ManagerException("Cannot add a tangent-linear model after finalization")  # noqa: E501

        if is_function(M):
            M = (M,)
        else:
            M = tuple(M)
        if is_function(dM):
            dM = (dM,)
        else:
            dM = tuple(dM)

        if (M, dM) in self._tlm:
            raise ManagerException("Duplicate tangent-linear model")

        if self._tlm_state == "initial":
            self._tlm_state = "deriving"
        elif self._tlm_state == "stopped_initial":
            self._tlm_state = "stopped_deriving"

        if len(M) == 1:
            tlm_map_name_suffix = \
                "_tlm(%s,%s)" % (function_name(M[0]),
                                 function_name(dM[0]))
        else:
            tlm_map_name_suffix = \
                "_tlm((%s),(%s))" % (",".join(function_name(m) for m in M),
                                     ",".join(function_name(dm) for dm in dM))
        self._tlm[(M, dM)] = (TangentLinearMap(tlm_map_name_suffix), max_depth)

    def tlm_enabled(self):
        """
        Return whether addition of tangent-linear models is enabled.
        """

        return self._tlm_state == "deriving"

    def tlm(self, M, dM, x):
        """
        Return a tangent-linear Function associated with the forward Function
        x, for the tangent-linear model defined by M and dM.
        """

        if is_function(M):
            M = (M,)
        else:
            M = tuple(M)
        if is_function(dM):
            dM = (dM,)
        else:
            dM = tuple(dM)

        if (M, dM) in self._tlm:
            if x in self._tlm[(M, dM)][0]:
                return self._tlm[(M, dM)][0][x]
            else:
                raise ManagerException("Tangent-linear not found")
        else:
            raise ManagerException("Tangent-linear not found")

    def annotation_enabled(self):
        """
        Return whether the equation manager currently has annotation enabled.
        """

        return self._annotation_state in ["initial", "annotating"]

    def start(self, annotation=True, tlm=True):
        """
        Start annotation or tangent-linear derivation.
        """

        if annotation:
            if self._annotation_state == "stopped_initial":
                self._annotation_state = "initial"
            elif self._annotation_state == "stopped_annotating":
                self._annotation_state = "annotating"

        if tlm:
            if self._tlm_state == "stopped_initial":
                self._tlm_state = "initial"
            elif self._tlm_state == "stopped_deriving":
                self._tlm_state = "deriving"

    def stop(self, annotation=True, tlm=True):
        """
        Pause annotation or tangent-linear derivation. Returns a tuple
        containing:
            (annotation_state, tlm_state)
        where annotation_state is True if the annotation is in state "initial"
        or "annotating" and False otherwise, and tlm_state is True if the
        tangent-linear state is "initial" or "deriving" and False otherwise,
        each evaluated before changing the state.
        """

        state = (self._annotation_state in ["initial", "annotating"],
                 self._tlm_state in ["initial", "deriving"])

        if annotation:
            if self._annotation_state == "initial":
                self._annotation_state = "stopped_initial"
            elif self._annotation_state == "annotating":
                self._annotation_state = "stopped_annotating"

        if tlm:
            if self._tlm_state == "initial":
                self._tlm_state = "stopped_initial"
            elif self._tlm_state == "deriving":
                self._tlm_state = "stopped_deriving"

        return state

    def add_initial_condition(self, x, annotate=None):
        """
        Add an initial condition associated with the Function x on the adjoint
        tape.

        annotate (default self.annotation_enabled()):
            Whether to annotate the initial condition on the adjoint tape,
            storing data for checkpointing as required.
        """

        if annotate is None:
            annotate = self.annotation_enabled()
        if annotate:
            if self._annotation_state == "initial":
                self._annotation_state = "annotating"
            elif self._annotation_state == "stopped_initial":
                self._annotation_state = "stopped_annotating"
            elif self._annotation_state == "final":
                raise ManagerException("Cannot add initial conditions after finalization")  # noqa: E501

            self._cp.add_initial_condition(x)

    def initial_condition(self, x):
        """
        Return the value of the initial condition for x recorded for the first
        block. Finalizes the manager.
        """

        self.finalize()

        x_id = x.id()
        for eq in self._blocks[0]:
            if x_id in set(dep.id() for dep in eq.dependencies()):
                self._restore_checkpoint(0)
                return self._cp.initial_condition(x)
        raise ManagerException("Initial condition not found")

    def add_equation(self, eq, annotate=None, tlm=None, tlm_skip=None):
        """
        Process the provided equation, annotating and / or deriving (and
        solving) tangent-linear equations as required. Assumes that the
        equation has already been solved, and that the initial condition for
        eq.X() has been recorded on the adjoint tape if necessary.

        annotate (default self.annotation_enabled()):
            Whether to annotate the equation on the adjoint tape, storing data
            for checkpointing as required.
        tlm (default self.tlm_enabled()):
            Whether to derive (and solve) associated tangent-linear equations.
        tlm_skip (default None):
            Used for the derivation of higher order tangent-linear equations.
        """

        if annotate is None:
            annotate = self.annotation_enabled()
        if annotate:
            if self._annotation_state == "initial":
                self._annotation_state = "annotating"
            elif self._annotation_state == "stopped_initial":
                self._annotation_state = "stopped_annotating"
            elif self._annotation_state == "final":
                raise ManagerException("Cannot add equations after finalization")  # noqa: E501

            if self._cp_method == "memory" and not self._cp_parameters["replace"]:  # noqa: E501
                self._block.append(eq)
            else:
                eq_alias = EquationAlias(eq)
                eq_id = eq.id()
                if eq_id not in self._finalizes:
                    def callback(self_ref, eq_ref):
                        self = self_ref()
                        eq = eq_ref()
                        if self is not None and eq is not None:
                            self.replace(eq)
                    self._finalizes[eq_id] = weakref.finalize(
                        eq, callback, weakref.ref(self), weakref.ref(eq_alias))
                self._block.append(eq_alias)
            self._cp.add_equation(
                (len(self._blocks), len(self._block) - 1), eq)

        if tlm is None:
            tlm = self.tlm_enabled()
        if tlm:
            if self._tlm_state == "final":
                raise ManagerException("Cannot add tangent-linear equations after finalization")  # noqa: E501

            X = eq.X()
            depth = 0 if tlm_skip is None else tlm_skip[1]
            for i, (M, dM) in enumerate(reversed(self._tlm)):
                if tlm_skip is not None and i >= tlm_skip[0]:
                    break
                tlm_map, max_depth = self._tlm[(M, dM)]
                eq_tlm_eqs = self._tlm_eqs.get(eq.id(), None)
                if eq_tlm_eqs is None:
                    eq_tlm_eqs = self._tlm_eqs[eq.id()] = {}
                tlm_eq = eq_tlm_eqs.get((M, dM), None)
                if tlm_eq is None:
                    for dep in eq.dependencies():
                        if dep in M or dep in tlm_map:
                            if len(set(X).intersection(set(M))) > 0:
                                raise ManagerException("Invalid tangent-linear parameter")  # noqa: E501
                            tlm_eq = eq.tangent_linear(M, dM, tlm_map)
                            if tlm_eq is None:
                                tlm_eq = NullSolver([tlm_map[x] for x in X])
                            eq_tlm_eqs[(M, dM)] = tlm_eq
                            break
                if tlm_eq is not None:
                    tlm_eq.solve(
                        manager=self, annotate=annotate, tlm=True,
                        _tlm_skip=([i + 1, depth + 1] if max_depth - depth > 1
                                   else [i, 0]))

    def replace(self, eq):
        """
        Replace internal Function objects in the provided equation with
        ReplacementFunction objects.
        """

        eq_id = eq.id()
        if eq_id in self._replaced:
            return
        self._replaced.add(eq_id)

        deps = eq.dependencies()
        for dep in deps:
            dep_id = dep.id()
            if dep_id not in self._replace_map:
                replaced_dep = replaced_function(dep)
                self._replace_map[dep_id] = replaced_dep
        eq.replace({dep: self._replace_map[dep.id()] for dep in deps})
        if eq_id in self._tlm_eqs:
            for tlm_eq in self._tlm_eqs[eq_id].values():
                if tlm_eq is not None:
                    self.replace(tlm_eq)

    def map(self, x):
        return self._replace_map.get(x.id(), x)

    def _checkpoint_space_id(self, fn):
        space = fn.function_space()
        space_id = function_space_id(space)
        if space_id not in self._cp_spaces:
            self._cp_spaces[space_id] = space
        return space_id

    def _save_memory_checkpoint(self, cp, n):
        self._cp_memory[n] = self._cp.initial_conditions(cp=True, refs=False,
                                                         copy=False)

    def _load_memory_checkpoint(self, storage, n, delete=False):
        if delete:
            storage.update(self._cp_memory.pop(n), copy=False)
        else:
            storage.update(self._cp_memory[n], copy=True)

    def _save_disk_checkpoint(self, cp, n):
        cp_path = self._cp_parameters["path"]
        cp_format = self._cp_parameters["format"]

        cp = self._cp.initial_conditions(cp=True, refs=False, copy=False)

        if cp_format == "pickle":
            cp_filename = os.path.join(
                cp_path,
                "checkpoint_%i_%i_%i_%i.pickle" % (self._id,
                                                   n,
                                                   self._comm_py2f,
                                                   self._comm.rank))
            h = open(cp_filename, "wb")

            pickle.dump({key: (self._checkpoint_space_id(F),
                               function_get_values(F))
                         for key, F in cp.items()},
                        h, protocol=pickle.HIGHEST_PROTOCOL)

            h.close()
        elif cp_format == "hdf5":
            cp_filename = os.path.join(
                cp_path,
                "checkpoint_%i_%i_%i.hdf5" % (self._id,
                                              n,
                                              self._comm_py2f))
            import h5py
            if self._comm.size > 1:
                h = h5py.File(cp_filename, "w", driver="mpio", comm=self._comm)
            else:
                h = h5py.File(cp_filename, "w")

            h.create_group("/ics")
            for i, (key, F) in enumerate(cp.items()):
                g = h.create_group("/ics/%i" % i)

                values = function_get_values(F)
                d = g.create_dataset("value", shape=(function_global_size(F),),
                                     dtype=values.dtype)
                d[function_local_indices(F)] = values
                del(values)

                d = g.create_dataset("space_id", shape=(self._comm.size,),
                                     dtype=np.int64)
                d[self._comm.rank] = self._checkpoint_space_id(F)

                d = g.create_dataset("key", shape=(self._comm.size,),
                                     dtype=np.int64)
                d[self._comm.rank] = key

            h.close()
        else:
            raise ManagerException(f"Unrecognized checkpointing format: {cp_format:s}")  # noqa: E501

    def _load_disk_checkpoint(self, storage, n, delete=False):
        cp_path = self._cp_parameters["path"]
        cp_format = self._cp_parameters["format"]

        if cp_format == "pickle":
            cp_filename = os.path.join(
                cp_path,
                "checkpoint_%i_%i_%i_%i.pickle" % (self._id,
                                                   n,
                                                   self._comm_py2f,
                                                   self._comm.rank))
            h = open(cp_filename, "rb")
            cp = pickle.load(h)
            h.close()
            if delete:
                if self._comm.rank == 0:
                    os.remove(cp_filename)
                self._comm.barrier()

            for key in tuple(cp.keys()):
                space_id, values = cp.pop(key)
                if key in storage:
                    F = function_space_new(self._cp_spaces[space_id])
                    function_set_values(F, values)
                    storage[key] = F
                del(space_id, values)
        elif cp_format == "hdf5":
            cp_filename = os.path.join(
                cp_path,
                "checkpoint_%i_%i_%i.hdf5" % (self._id,
                                              n,
                                              self._comm_py2f))
            import h5py
            if self._comm.size > 1:
                h = h5py.File(cp_filename, "r", driver="mpio", comm=self._comm)
            else:
                h = h5py.File(cp_filename, "r")

            for name, g in h["/ics"].items():
                d = g["key"]
                key = int(d[self._comm.rank])
                if key in storage:
                    d = g["space_id"]
                    F = function_space_new(self._cp_spaces[d[self._comm.rank]])
                    d = g["value"]
                    function_set_values(F, d[function_local_indices(F)])
                    storage[key] = F
                del(g, d)

            h.close()
            if delete:
                if self._comm.rank == 0:
                    os.remove(cp_filename)
                self._comm.barrier()
        else:
            raise ManagerException(f"Unrecognized checkpointing format: {cp_format:s}")  # noqa: E501

    def _checkpoint(self, final=False):
        if self._cp_method == "memory":
            pass
        elif self._cp_method == "periodic_disk":
            self._periodic_disk_checkpoint(final=final)
        elif self._cp_method == "multistage":
            self._multistage_checkpoint()
        else:
            raise ManagerException(f"Unrecognized checkpointing method: {self._cp_method:s}")  # noqa: E501

    def _periodic_disk_checkpoint(self, final=False):
        cp_period = self._cp_parameters["period"]

        n = len(self._blocks) - 1
        if final or n % cp_period == cp_period - 1:
            self._save_disk_checkpoint(self._cp,
                                       n=(n // cp_period) * cp_period)
            self._cp.clear()
            self._cp.configure(store_ics=True,
                               store_data=False)

    def _save_multistage_checkpoint(self):
        def debug_info(message):
            if self._cp_parameters["verbose"]:
                info(message)

        deferred_snapshot = self._cp_manager.deferred_snapshot()
        if deferred_snapshot is not None:
            snapshot_n, snapshot_storage = deferred_snapshot
            if snapshot_storage == "disk":
                if self._cp_manager.r() == 0:
                    debug_info(f"forward: save snapshot at {snapshot_n:d} on disk")  # noqa: E501
                else:
                    debug_info(f"reverse: save snapshot at {snapshot_n:d} on disk")  # noqa: E501
                self._save_disk_checkpoint(self._cp, snapshot_n)
            else:
                if self._cp_manager.r() == 0:
                    debug_info(f"forward: save snapshot at {snapshot_n:d} in RAM")  # noqa: E501
                else:
                    debug_info(f"reverse: save snapshot at {snapshot_n:d} in RAM")  # noqa: E501
                self._save_memory_checkpoint(self._cp, snapshot_n)

    def _multistage_checkpoint(self):
        def debug_info(message):
            if self._cp_parameters["verbose"]:
                info(message)

        n = len(self._blocks)
        if n < self._cp_manager.n():
            return
        elif n == self._cp_manager.max_n():
            return
        elif n > self._cp_manager.max_n():
            raise ManagerException("Unexpected number of blocks")

        self._save_multistage_checkpoint()
        self._cp.clear()
        if n == self._cp_manager.max_n() - 1:
            debug_info("forward: configuring storage for reverse")
            self._cp.configure(store_ics=False,
                               store_data=True)
        else:
            debug_info("forward: configuring storage for snapshot")
            self._cp.configure(store_ics=True,
                               store_data=False)
            debug_info(f"forward: deferred snapshot at {self._cp_manager.n():d}")  # noqa: E501
            self._cp_manager.snapshot()
        self._cp_manager.forward()
        debug_info(f"forward: forward advance to {self._cp_manager.n():d}")

    def _restore_checkpoint(self, n):
        if self._cp_method == "memory":
            pass
        elif self._cp_method == "periodic_disk":
            if n not in self._cp_manager:
                cp_period = self._cp_parameters["period"]

                N0 = (n // cp_period) * cp_period
                N1 = min(((n // cp_period) + 1) * cp_period, len(self._blocks))

                self._cp.clear()
                self._cp_manager.clear()
                storage = ReplayStorage(self._blocks, N0, N1)
                storage.update(self._cp.initial_conditions(cp=False, refs=True,
                                                           copy=False),
                               copy=False)
                self._load_disk_checkpoint(storage, N0, delete=False)

                for n1 in range(N0, N1):
                    self._cp.configure(store_ics=n1 == 0,
                                       store_data=True)

                    for i, eq in enumerate(self._blocks[n1]):
                        eq_deps = eq.dependencies()

                        X = tuple(storage[eq_x] for eq_x in eq.X())
                        deps = tuple(storage[eq_dep] for eq_dep in eq_deps)

                        for eq_dep in eq.initial_condition_dependencies():
                            self._cp.add_initial_condition(
                                eq_dep, value=storage[eq_dep])
                        eq.forward(X, deps=deps)
                        self._cp.add_equation((n1, i), eq, deps=deps)

                        storage.pop()

                    self._cp_manager.add(n1)
                assert(len(storage) == 0)
        elif self._cp_method == "multistage":
            def debug_info(message):
                if self._cp_parameters["verbose"]:
                    info(message)

            if n == 0 and self._cp_manager.max_n() - self._cp_manager.r() == 0:
                return
            elif n == self._cp_manager.max_n() - 1:
                debug_info(f"reverse: adjoint step back to {n:d}")
                self._cp_manager.reverse()
                return

            (snapshot_n,
             snapshot_storage,
             snapshot_delete) = self._cp_manager.load_snapshot()
            self._cp.clear()
            storage = ReplayStorage(self._blocks, snapshot_n, n + 1)
            storage.update(self._cp.initial_conditions(cp=False, refs=True,
                                                       copy=False),
                           copy=False)
            if snapshot_storage == "disk":
                debug_info(f'reverse: load snapshot at {snapshot_n:d} from disk and {"delete" if snapshot_delete else "keep":s}')  # noqa: E501
                self._load_disk_checkpoint(storage, snapshot_n,
                                           delete=snapshot_delete)
            else:
                debug_info(f'reverse: load snapshot at {snapshot_n:d} from RAM and {"delete" if snapshot_delete else "keep":s}')  # noqa: E501
                self._load_memory_checkpoint(storage, snapshot_n,
                                             delete=snapshot_delete)

            if snapshot_n < n:
                debug_info("reverse: no storage")
                self._cp.configure(store_ics=False,
                                   store_data=False)

            snapshot_n_0 = snapshot_n
            while True:
                if snapshot_n == n:
                    debug_info("reverse: configuring storage for reverse")
                    self._cp.configure(store_ics=n == 0,
                                       store_data=True)
                elif snapshot_n > snapshot_n_0:
                    debug_info("reverse: configuring storage for snapshot")
                    self._cp .configure(store_ics=True,
                                        store_data=False)
                    debug_info(f"reverse: deferred snapshot at {self._cp_manager.n():d}")  # noqa: E501
                    self._cp_manager.snapshot()
                self._cp_manager.forward()
                debug_info(f"reverse: forward advance to {self._cp_manager.n():d}")  # noqa: E501
                for n1 in range(snapshot_n, self._cp_manager.n()):
                    for i, eq in enumerate(self._blocks[n1]):
                        eq_deps = eq.dependencies()

                        X = tuple(storage[eq_x] for eq_x in eq.X())
                        deps = tuple(storage[eq_dep] for eq_dep in eq_deps)

                        for eq_dep in eq.initial_condition_dependencies():
                            self._cp.add_initial_condition(
                                eq_dep, value=storage[eq_dep])
                        eq.forward(X, deps=deps)
                        self._cp.add_equation((n1, i), eq, deps=deps)

                        storage.pop()
                snapshot_n = self._cp_manager.n()
                if snapshot_n > n:
                    break
                self._save_multistage_checkpoint()
                self._cp.clear()
            assert(len(storage) == 0)

            debug_info(f"reverse: adjoint step back to {n:d}")
            self._cp_manager.reverse()
        else:
            raise ManagerException(f"Unrecognized checkpointing method: {self._cp_method:s}")  # noqa: E501

    def new_block(self):
        """
        End the current block equation and begin a new block. Ignored if
        "multistage" checkpointing is used and the final block has been
        reached.
        """

        if self._annotation_state in ["stopped_initial",
                                      "stopped_annotating",
                                      "final"]:
            return
        elif self._cp_method == "multistage" \
                and len(self._blocks) == self._cp_parameters["blocks"] - 1:
            # Wait for the finalize
            warning("Attempting to end the final block without finalising -- ignored")  # noqa: E501
            return

        self._blocks.append(self._block)
        self._block = []
        self._checkpoint(final=False)

    def finalize(self):
        """
        End the final block equation.
        """

        if self._annotation_state == "final":
            return
        self._annotation_state = "final"
        self._tlm_state = "final"

        self._blocks.append(self._block)
        self._block = []
        self._checkpoint(final=True)

    def dependency_graph_png(self, divider=[255, 127, 127], p=5):
        P = 2 ** p

        blocks = copy.copy(self._blocks)
        if len(self._block) > 0:
            blocks.append(self._block)

        M = 0
        for block in blocks:
            M += len(block) * P
        M += len(blocks) + 1
        pixels = np.empty((M, M, 3), dtype=np.uint8)
        pixels[:] = 255

        pixels[0, :, :] = divider
        pixels[:, 0, :] = divider
        index = 1
        for block in blocks:
            pixels[index + len(block) * P, :, :] = divider
            pixels[:, index + len(block) * P, :] = divider
            index += len(block) * P + 1

        index = 1
        dep_map = {}
        for block in blocks:
            for eq in block:
                eq_indices = slice(index, index + P)
                for x in eq.X():
                    dep_map[x.id()] = eq_indices
                index += P
                for dep in eq.dependencies():
                    dep_id = dep.id()
                    if dep_id in dep_map:
                        pixels[eq_indices, dep_map[dep_id]] = 0
            index += 1

        import png
        return png.from_array(pixels, "RGB")

    def compute_gradient(self, Js, M, callback=None):
        """
        Compute the derivative of one or more functionals with respect to one
        or more control parameters by running adjoint models. Finalizes the
        manager.

        Arguments:

        Js        A Functional or Function, or a list or tuple of these,
                  defining the functionals.
        M         A Control or Function, or a list or tuple of these, defining
                  the control parameters.
        callback  (Optional) Callable of the form
                      def callback(J_i, n, i, eq, adj_X):
                  where adj_X is None, a Function, or a list or tuple of
                  Function objects, corresponding to the adjoint solution for
                  the equation eq, which is equation i in block n for the
                  J_i th Functional.
        """

        if not isinstance(M, (list, tuple)):
            if not isinstance(Js, (list, tuple)):
                ((dJ,),) = self.compute_gradient([Js], [M], callback=callback)
                return dJ
            else:
                dJs = self.compute_gradient(Js, [M], callback=callback)
                return tuple(dJ for (dJ,) in dJs)
        elif not isinstance(Js, (list, tuple)):
            dJ, = self.compute_gradient([Js], M, callback=callback)
            return dJ

        self.finalize()

        # Functionals
        Js = tuple(J.fn() if not is_function(J) else J for J in Js)

        # Controls
        M = tuple(m if is_function(m) else m.m() for m in M)

        # Derivatives
        dJ = [None for J in Js]

        # Add two additional blocks, one at the start and one at the end of the
        # forward:
        #   Control block   :  Represents the equation "controls = inputs"
        #   Functional block:  Represents the equations "outputs = functionals"
        blocks = ([[ControlsMarker(M)]]
                  + self._blocks
                  + [[FunctionalMarker(J) for J in Js]])

        # Adjoint equation right-hand-sides
        Bs = tuple(AdjointModelRHS(blocks) for J in Js)
        # Adjoint initial condition
        for J_i in range(len(Js)):
            function_assign(Bs[J_i][-1][J_i].b(), 1.0)

        # Transposed dependency graph information
        tdeps = DependencyTransposer(blocks, M)

        # Reverse (blocks)
        seen_eq_ids = set()
        for n in range(len(blocks) - 1, -1, -1):
            cp_n = n - 1  # Forward model block, ignoring the control block
            cp_block = cp_n >= 0 and cp_n < len(self._blocks)
            if cp_block:
                # Load/restore forward model data
                self._restore_checkpoint(cp_n)

            # Reverse (equations in block n)
            for i in range(len(blocks[n]) - 1, -1, -1):
                eq = blocks[n][i]
                eq_id = eq.id()
                if eq_id not in seen_eq_ids:
                    seen_eq_ids.add(eq_id)
                    eq.reset_adjoint()
                # Non-linear dependency data
                nl_deps = self._cp[(cp_n, i)] if cp_block else tuple()

                # Transposed dependency graph information for this equation
                B_indices = {}
                for j, dep in enumerate(eq.dependencies()):
                    if dep in tdeps:
                        p, k, m = tdeps[dep]
                        if p != n or k != i:
                            B_indices[j] = (p, k, m)
                # Clear dependency information for this equation
                tdeps.pop()

                for J_i, J in enumerate(Js):
                    # Adjoint model right-hand-sides
                    B = Bs[J_i]
                    # Adjoint right-hand-side associated with this equation
                    eq_B = B.pop()

                    # Zero right-hand-side, adjoint solution is zero, or the
                    # sensitivity does not depend on the adjoint solution
                    # associated with this equation
                    if eq_B.is_empty():
                        adj_X = None
                    else:
                        # Solve adjoint equation, add terms to adjoint
                        # equations
                        adj_X = eq.adjoint(J, nl_deps, eq_B.B(), B_indices, B)
                    if callback is not None and cp_block:
                        if adj_X is None or len(adj_X) > 1:
                            callback(J_i, cp_n, i, eq, adj_X)
                        else:
                            callback(J_i, cp_n, i, eq, adj_X[0])

                    if n == 0 and i == 0:
                        # A requested derivative
                        if adj_X is None:
                            dJ[J_i] = tuple(function_new(m) for m in M)
                        else:
                            dJ[J_i] = tuple(function_copy(adj_x)
                                            for adj_x in adj_X)

            if n > 0:
                # Force finalization of right-hand-sides in the control block
                for B in Bs:
                    B[0].finalize()

        for B in Bs:
            assert(B.is_empty())
        assert(tdeps.is_empty())

        if self._cp_method == "multistage":
            self._cp.clear(clear_cp=False, clear_data=True, clear_refs=False)

        return tuple(dJ)

    def find_initial_condition(self, x):
        """
        Find the initial condition Function or ReplacementFunction associated
        with the given Function or name.
        """

        if is_function(x):
            return self.map(x)
        else:
            for block in self._blocks + [self._block]:
                for eq in block:
                    for dep in eq.dependencies():
                        if function_name(dep) == x:
                            return dep
            raise ManagerException("Initial condition not found")


set_manager(EquationManager())


def configure_checkpointing(cp_method, cp_parameters={}, manager=None):
    if manager is None:
        manager = _manager()
    manager.configure_checkpointing(cp_method, cp_parameters=cp_parameters)


def manager_info(info=info, manager=None):
    if manager is None:
        manager = _manager()
    manager.info(info=info)


def reset_manager(cp_method=None, cp_parameters=None, manager=None):
    if manager is None:
        manager = _manager()
    manager.reset(cp_method=cp_method, cp_parameters=cp_parameters)


def reset(cp_method=None, cp_parameters=None, manager=None):
    if manager is None:
        manager = _manager()
    manager.reset(cp_method=cp_method, cp_parameters=cp_parameters)


def annotation_enabled(manager=None):
    if manager is None:
        manager = _manager()
    return manager.annotation_enabled()


def start_manager(annotation=True, tlm=True, manager=None):
    if manager is None:
        manager = _manager()
    manager.start(annotation=annotation, tlm=tlm)


def start_annotating(manager=None):
    if manager is None:
        manager = _manager()
    manager.start(annotation=True, tlm=False)


def start_tlm(manager=None):
    if manager is None:
        manager = _manager()
    manager.start(annotation=False, tlm=True)


def stop_manager(annotation=True, tlm=True, manager=None):
    if manager is None:
        manager = _manager()
    manager.stop(annotation=annotation, tlm=tlm)


def stop_annotating(manager=None):
    if manager is None:
        manager = _manager()
    manager.stop(annotation=True, tlm=False)


def stop_tlm(manager=None):
    if manager is None:
        manager = _manager()
    manager.stop(annotation=False, tlm=True)


def add_tlm(M, dM, max_depth=1, manager=None):
    if manager is None:
        manager = _manager()
    manager.add_tlm(M, dM, max_depth=max_depth)


def tlm_enabled(manager=None):
    if manager is None:
        manager = _manager()
    return manager.tlm_enabled()


def tlm(M, dM, x, manager=None):
    if manager is None:
        manager = _manager()
    return manager.tlm(M, dM, x)


def compute_gradient(Js, M, callback=None, manager=None):
    if manager is None:
        manager = _manager()
    return manager.compute_gradient(Js, M, callback=callback)


def new_block(manager=None):
    if manager is None:
        manager = _manager()
    manager.new_block()


def minimize_scipy(forward, M0, J0=None, manager=None, **kwargs):
    """
    Gradient-based minimization using scipy.optimize.minimize.

    Arguments:

    forward  A callable which takes as input the control and returns the
             Functional to be minimized.
    M0       A Function, or a list or tuple of Function objects. Control
             parameters initial guess.
    J0       (Optional) Initial functional. If supplied assumes that the
             forward has already been run, and processed by the equation
             manager, using the control parameters given by M0.
    manager  (Optional) The equation manager.

    Any remaining keyword arguments are passed directly to
    scipy.optimize.minimize.

    Returns a tuple
        (M, return_value)
    return M is the value of the control parameters obtained, and return_value
    is the return value of scipy.optimize.minimize.
    """

    if not isinstance(M0, (list, tuple)):
        (M,), return_value = minimize_scipy(forward, [M0], J0=J0,
                                            manager=manager, **kwargs)
        return M, return_value

    M0 = [m0 if is_function(m0) else m0.m() for m0 in M0]
    if manager is None:
        manager = _manager()
    comm = manager.comm()

    N = [0]
    for m in M0:
        N.append(N[-1] + function_local_size(m))
    size_global = comm.allgather(np.array(N[-1], dtype=np.int64))
    N_global = [0]
    for size in size_global:
        N_global.append(N_global[-1] + size)

    def get(F):
        x = np.empty(N[-1], dtype=np.float64)
        for i, f in enumerate(F):
            x[N[i]:N[i + 1]] = function_get_values(f)

        x_global = comm.allgather(x)
        X = np.empty(N_global[-1], dtype=np.float64)
        for i, x_p in enumerate(x_global):
            X[N_global[i]:N_global[i + 1]] = x_p
        return X

    def set(F, x):
        # Basic cross-process synchonization check
        check1 = np.array(zlib.adler32(x.data), dtype=np.uint32)
        check_global = comm.allgather(check1)
        for check2 in check_global:
            if check1 != check2:
                raise ManagerException("Parallel desynchronization detected")

        x = x[N_global[comm.rank]:N_global[comm.rank + 1]]
        for i, f in enumerate(F):
            function_set_values(f, x[N[i]:N[i + 1]])

    M = [function_new(m0, static=function_is_static(m0),
                      cache=function_is_cached(m0),
                      checkpoint=function_is_checkpointed(m0))
         for m0 in M0]
    J = [J0]
    J_M = [M0]

    def fun(x):
        if not J[0] is None:
            return J[0].value()

        set(M, x)
        old_manager = _manager()
        set_manager(manager)
        manager.reset()
        manager.stop()
        clear_caches()

        manager.start()
        J[0] = forward(*M)
        manager.stop()

        set_manager(old_manager)

        J_M[0] = M
        return J[0].value()

    def jac(x):
        fun(x)
        dJ = manager.compute_gradient(J[0], J_M[0])
        J[0] = None
        return get(dJ)

    from scipy.optimize import minimize
    return_value = minimize(fun, get(M0), jac=jac, **kwargs)
    set(M, return_value.x)

    return M, return_value


def taylor_test(forward, M, J_val, dJ=None, ddJ=None, seed=1.0e-2, dM=None,
                M0=None, size=5, manager=None):
    # Aims for similar behaviour to the dolfin-adjoint taylor_test function in
    # dolfin-adjoint 2017.1.0. Arguments based on dolfin-adjoint taylor_test
    # arguments
    #   forward (renamed from J)
    #   M (renamed from m)
    #   J_val (renamed from Jm)
    #   dJ (renamed from dJdm)
    #   ddJ (renamed from HJm)
    #   seed
    #   dM (renamed from perturbation_direction)
    #   M0 (renamed from value)
    #   size
    """
    Perform a Taylor remainder verification test.

    Arguments:

    forward  A callable which takes as input one or more Function objects
             defining the value of the control, and returns the Functional.
    M        A Control or Function, or a list or tuple of these. The control.
    J_val    The reference functional value.
    dJ       (Optional if ddJ is supplied) A Function, or a list or tuple of
             Function objects, storing the derivative of J with respect to M.
    ddJ      (Optional) A Hessian used to compute Hessian actions associated
             with the second derivative of J with respect to M.
    seed     (Optional) The maximum scaling for the perturbation is seed
             multiplied by the inf norm of the reference value (degrees of
             freedom inf norm) of the control (or 1 if this is less than 1).
    dM       A perturbation direction. Values generated using
             numpy.random.random are used if not supplied.
    M0       (Optional) The reference value of the control.
    size     (Optional) The number of perturbed forward runs used in the test.
    manager  (Optional) The equation manager.
    """

    if not isinstance(M, (list, tuple)):
        if dJ is not None:
            dJ = [dJ]
        if dM is not None:
            dM = [dM]
        if M0 is not None:
            M0 = [M0]
        return taylor_test(forward, [M], J_val, dJ=dJ, ddJ=ddJ, seed=seed,
                           dM=dM, M0=M0, size=size, manager=manager)

    if manager is None:
        manager = _manager()

    M = [m.m() if not is_function(m) else m for m in M]
    if M0 is None:
        M0 = [manager.initial_condition(m) for m in M]
    M1 = [function_new(m, static=function_is_static(m),
                       cache=function_is_cached(m),
                       checkpoint=function_is_checkpointed(m))
          for m in M]

    def functions_inner(X, Y):
        inner = 0.0
        for x, y in zip(X, Y):
            inner += function_inner(x, y)
        return inner

    def functions_linf_norm(X):
        norm = 0.0
        for x in X:
            norm = max(norm, function_linf_norm(x))
        return norm

    # This combination seems to reproduce dolfin-adjoint behaviour
    eps = np.array([2 ** -p for p in range(size)], dtype=np.float64)
    eps = seed * eps * max(1.0, functions_linf_norm(M0))
    if dM is None:
        dM = [function_new(m1, static=True) for m1 in M1]
        for dm in dM:
            function_set_values(dm, np.random.random(function_local_size(dm)))

    J_vals = np.empty(eps.shape, dtype=np.float64)
    for i in range(eps.shape[0]):
        for m0, m1, dm in zip(M0, M1, dM):
            function_assign(m1, m0)
            function_axpy(m1, eps[i], dm)
        clear_caches()
        annotation_enabled, tlm_enabled = manager.stop()
        J_vals[i] = forward(*M1).value()
        manager.start(annotation=annotation_enabled, tlm=tlm_enabled)

    error_norms_0 = abs(J_vals - J_val)
    orders_0 = np.log(error_norms_0[1:] / error_norms_0[:-1]) / np.log(0.5)
    info(f"Error norms, no adjoint   = {error_norms_0}")
    info(f"Orders,      no adjoint   = {orders_0}")

    if ddJ is None:
        error_norms_1 = abs(J_vals - J_val
                            - eps * functions_inner(dJ, dM))
        orders_1 = np.log(error_norms_1[1:] / error_norms_1[:-1]) / np.log(0.5)
        info(f"Error norms, with adjoint = {error_norms_1}")
        info(f"Orders,      with adjoint = {orders_1}")
        return orders_1.min()
    else:
        if dJ is None:
            _, dJ, ddJ = ddJ.action(M0, dM)
        else:
            dJ = functions_inner(dJ, dM)
            _, _, ddJ = ddJ.action(M0, dM)
        error_norms_2 = abs(J_vals - J_val
                            - eps * dJ
                            - 0.5 * eps * eps * functions_inner(ddJ, dM))
        orders_2 = np.log(error_norms_2[1:] / error_norms_2[:-1]) / np.log(0.5)
        info(f"Error norms, with adjoint = {error_norms_2}")
        info(f"Orders,      with adjoint = {orders_2}")
        return orders_2.min()


def taylor_test_tlm(forward, M, tlm_order, seed=1.0e-2, dMs=None, size=5,
                    manager=None):
    if not isinstance(M, (list, tuple)):
        if dMs is not None:
            dMs = tuple((dM,) for dM in dMs)
        return taylor_test_tlm(forward, [M], tlm_order, seed=seed, dMs=dMs,
                               size=size, manager=manager)

    if manager is None:
        manager = _manager()
    tlm_manager = manager.new("memory", {})
    tlm_manager.stop()

    M1 = [function_new(m, static=function_is_static(m),
                       cache=function_is_cached(m),
                       checkpoint=function_is_checkpointed(m))
          for m in M]

    def functions_linf_norm(X):
        norm = 0.0
        for x in X:
            norm = max(norm, function_linf_norm(x))
        return norm

    eps = np.array([2 ** -p for p in range(size)], dtype=np.float64)
    eps = seed * eps * max(1.0, functions_linf_norm(M))
    if dMs is None:
        dMs = tuple(tuple(function_new(m, static=True) for m in M)
                    for i in range(tlm_order))
        for dM in dMs:
            for dm in dM:
                function_set_values(dm,
                                    np.random.random(function_local_size(dm)))

    def forward_tlm(dMs, *M):
        old_manager = _manager()
        set_manager(tlm_manager)
        tlm_manager.reset()
        tlm_manager.stop()
        clear_caches()

        for dM in dMs:
            tlm_manager.add_tlm(M, dM)
        tlm_manager.start(annotation=False, tlm=True)
        J = forward(*M)
        for dM in dMs:
            J = J.tlm(M, dM, manager=tlm_manager)

        set_manager(old_manager)

        return J

    J_val = forward_tlm(dMs[:-1], *M).value()
    dJ = forward_tlm(dMs, *M).value()

    J_vals = np.empty(eps.shape, dtype=np.float64)
    for i in range(eps.shape[0]):
        for m0, m1, dm in zip(M, M1, dMs[-1]):
            function_assign(m1, m0)
            function_axpy(m1, eps[i], dm)
        clear_caches()
        J_vals[i] = forward_tlm(dMs[:-1], *M1).value()

    error_norms_0 = abs(J_vals - J_val)
    orders_0 = np.log(error_norms_0[1:] / error_norms_0[:-1]) / np.log(0.5)
    info(f"Error norms, no adjoint   = {error_norms_0}")
    info(f"Orders,      no adjoint   = {orders_0}")

    error_norms_1 = abs(J_vals - J_val - eps * dJ)
    orders_1 = np.log(error_norms_1[1:] / error_norms_1[:-1]) / np.log(0.5)
    info(f"Error norms, with adjoint = {error_norms_1}")
    info(f"Orders,      with adjoint = {orders_1}")
    return orders_1.min()


def taylor_test_tlm_adjoint(forward, M, adjoint_order, seed=1.0e-2, dMs=None,
                            size=5, manager=None):
    if not isinstance(M, (list, tuple)):
        if dMs is not None:
            dMs = tuple((dM,) for dM in dMs)
        return taylor_test_tlm_adjoint(
            forward, [M], adjoint_order, seed=seed, dMs=dMs, size=size,
            manager=manager)

    if manager is None:
        manager = _manager()
    tlm_manager = manager.new()
    tlm_manager.stop()

    if dMs is None:
        dM_test = None
        dMs = tuple(tuple(function_new(m, static=True) for m in M)
                    for i in range(adjoint_order - 1))
        for dM in dMs:
            for dm in dM:
                function_set_values(dm,
                                    np.random.random(function_local_size(dm)))
    else:
        dM_test = dMs[-1]
        dMs = dMs[:-1]

    def forward_tlm(*M, annotation=False):
        old_manager = _manager()
        set_manager(tlm_manager)
        tlm_manager.reset()
        tlm_manager.stop()
        clear_caches()

        for dM in dMs:
            tlm_manager.add_tlm(M, dM)
        tlm_manager.start(annotation=annotation, tlm=True)
        J = forward(*M)
        for dM in dMs:
            J = J.tlm(M, dM, manager=tlm_manager)

        set_manager(old_manager)

        return J

    J = forward_tlm(*M, annotation=True)
    J_val = J.value()
    dJ = tlm_manager.compute_gradient(J, M)

    return taylor_test(forward_tlm, M, J_val, dJ=dJ, seed=seed, dM=dM_test,
                       size=size, manager=tlm_manager)
