"""
* Resue assembly when relevant (if no operator that maps to a specific block has been changed)
* Concatenate equations with the same sequence of operators
  - Should use the same discretization object
  - divergence operators on different grids considered the same
* Concatenated variables will share ad derivatives. However, it should be possible to combine
  subsets of variables with other variables (outside the set) to assemble different terms
* 
"""
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import scipy.sparse as sps

from . import operators, grid_operators
from .forward_mode import initAdArrays

import porepy as pp

__all__ = ["Equation", "EquationManager"]

grid_like_type = Union[pp.Grid, Tuple[pp.Grid, pp.Grid]]


class Equation:
    def __init__(self, operator, dof_manager: pp.Assembler, name: str = None):

        # Black sometimes formats long equations with parantheses in a way that is
        # interpreted as a tuple by Python. Sigh.
        if (
            isinstance(operator, tuple)
            and len(operator) == 1
            and isinstance(operator[0], operators.Operator)
        ):
            operator = operator[0]

        self._operator = operator

        self.name = name

        variable_dofs, variable_ids = self._identify_variables(dof_manager)

        self._variable_dofs = variable_dofs
        self._variable_ids = variable_ids

        self._stored_matrices = {}

    def local_dofs(self) -> np.ndarray:
        dofs = np.hstack([d for d in self._variable_dofs])
        return dofs

    def __repr__(self) -> str:
        return f"Equation named {self.name}"

    def _operators_from_gb(self):
        # Define operators from information in the GridBucket.
        # Legacy style definition
        discr_dict = {}

        bc_dict = {}

        for g, d in self._gb:
            loc_bc_dict = {}
            for variable, term in d[pp.DISCRETIZATION].items():  # terms per variable
                for key, discr in term.items():
                    if (discr, key) not in discr_dict:
                        # This discretization has not been encountered before.
                        # Make operators of all its discretization matrices

                        # Temporarily make an inner dictionary to store the individual
                        # discretizations. It would be neat if we could rather write
                        # something like op.flux, instead of op['flux']
                        op_dict = {}

                        for s in dir(discr):
                            if s.endswith("_matrix_key"):
                                matrix_key = s[:-11]
                                op = operators.Operator(discr, matrix_key, g)
                                op_dict[matrix_key] = op
                        discr_dict[(variable, key)] = op_dict

                    loc_bc_dict[discr.keyword] = grid_operators.BoundaryCondition(
                        discr.keyword
                    )
            bc_dict[g] = loc_bc_dict

        self._discretizations = discr_dict
        self._bc = bc_dict

    def _get_matrix(self, data, op):
        # Move this into a class

        discr = op._discr
        key = op._name
        mat_dict = data[pp.DISCRETIZATION_MATRICES][discr.keyword]
        mat_key = getattr(discr, key + "_matrix_key")
        return mat_dict[mat_key]

    def _find_subtree_variables(self, op: operators.Operator):
        if isinstance(op, operators.Variable) or isinstance(op, pp.ad.Variable):
            # We are at the bottom of the a branch of the tree
            return op
        else:
            # Look for variables among the children
            sub_variables = [
                self._find_subtree_variables(child) for child in op._tree._children
            ]
            # Some work is needed to parse the information
            var_list = []
            for var in sub_variables:
                if isinstance(var, operators.Variable) or isinstance(
                    var, pp.ad.Variable
                ):
                    # Effectively, this node is one step from the leaf
                    var_list.append(var)
                elif isinstance(var, list):
                    # We are further up in the tree.
                    for sub_var in var:
                        if isinstance(sub_var, operators.Variable) or isinstance(
                            sub_var, pp.ad.Variable
                        ):
                            var_list.append(sub_var)
            return var_list

    def _identify_variables(self, dof_manager):
        # NOTES TO SELF:
        # assembler -> dof_manager
        # gb: needed
        # state: state vector for all unknowns. Should be possible to pick this
        # from pp.STATE or pp.ITERATE

        # 1. Get all variables present in this equation
        # Uniquify by making this a set, and then sort on variable id
        variables = sorted(
            list(set(self._find_subtree_variables(self._operator))),
            key=lambda var: var.id,
        )

        # 2. Get state of the variables, init ad
        # Make the AD variables active of sorts; so that when parsing the individual
        # operators, we can access the right variables

        # For each variable, get the global index
        inds = []
        variable_ids = []
        for variable in variables:
            ind_var = []
            if isinstance(variable, pp.ad.MergedVariable):
                for i, sub_var in enumerate(variable.sub_vars):
                    ind_var.append(dof_manager.dof_ind(sub_var.g, sub_var._name))
                    if i == 0:
                        variable_ids.append(sub_var.id)
            else:
                # This is a variable that lives on a single grid
                ind_var.append(dof_manager.dof_ind(variable.g, variable._name))
                variable_ids.append(variable.id)

            inds.append(np.hstack([i for i in ind_var]))

        return inds, variable_ids

    def to_ad(self, assembler, gb, state):
        # Initialize variables
        ad_vars = initAdArrays([state[ind] for ind in self._variable_dofs])
        self._ad = {var_id: ad for (var_id, ad) in zip(self._variable_ids, ad_vars)}

        # 3. Parse operators. Matrices can be picked either from discretization matrices,
        # or from some central storage,
        eq = self._parse_operator(self._operator, assembler.gb)

        return eq

    def _parse_operator(self, op: operators.Operator, gb):
        """TODO: Currently, there is no prioritization between the operations; for
        some reason, things just work. We may need to make an ordering in which the
        operations should be carried out. It seems that the strategy of putting on
        hold until all children are processed works, but there likely are cases where
        this is not the case.
        """
        # Q: The parsing could also be moved to the operator classes
        tree = op._tree
        if isinstance(op, pp.ad.Variable) or isinstance(op, operators.Variable):
            assert len(tree._children) == 0
            # Need access to state, grids, assembler, local_dof etc.

            # Really need a method to get state in all variables to which this
            # should have a coupling.
            # should use all variables in this equation. Need to pick out the right part of
            # it here (perhaps as by indexing a list of variables) for use to propagete through
            # the chain of operations
            if isinstance(op, pp.ad.MergedVariable) or isinstance(
                op, operators.MergedVariable
            ):
                return self._ad[op.sub_vars[0].id]
            else:
                return self._ad[op.id]
        if isinstance(op, grid_operators.BoundaryCondition) or isinstance(
            op, pp.ad.BoundaryCondition
        ):
            val = []
            for g in op.g:
                data = gb.node_props(g)
                val.append(data[pp.PARAMETERS][op.keyword]["bc_values"])

            return np.hstack([v for v in val])

        if isinstance(op, pp.ad.Matrix):
            return op.mat

        if isinstance(op, pp.ad.Array):
            return op.values

        if isinstance(op, pp.ad.Function):
            return op

        if isinstance(op, pp.ad.Scalar):
            return op.value
        if isinstance(op, grid_operators.Divergence) or isinstance(
            op, pp.ad.Divergence
        ):
            if op.scalar:
                mat = [pp.fvutils.scalar_divergence(g) for g in op.g]
            else:
                mat = [pp.fvutils.vector_divergence(g) for g in op.g]
            matrix = sps.block_diag(mat)
            return matrix

        if len(tree._children) == 0:
            if isinstance(op, operators.MergedOperator):
                if op in self._stored_matrices:
                    return self._stored_matrices[op]
                else:

                    mat = []
                    for g, discr in op.grid_discr.items():
                        if isinstance(g, pp.Grid):
                            data = gb.node_props(g)
                        else:
                            data = gb.edge_props(g)
                        if hasattr(op, "mat_dict_key") and op.mat_dict_key is not None:
                            mat_dict_key = op.mat_dict_key
                        else:
                            mat_dict_key = discr.keyword
                        mat_dict = data[pp.DISCRETIZATION_MATRICES][mat_dict_key]

                        # Get the submatrix for the right discretization
                        key = op.key
                        mat_key = getattr(discr, key + "_matrix_key")
                        mat.append(mat_dict[mat_key])

                    matrix = sps.block_diag(mat)
                    self._stored_matrices[op] = matrix
                    return matrix
            else:
                # Single grid
                assert False
                return self._get_matrix(op.g, op)

        results = [self._parse_operator(child, gb) for child in tree._children]

        if tree._op == operators.Operation.add:
            assert len(results) == 2
            return results[0] + results[1]
        elif tree._op == operators.Operation.sub:
            assert len(results) == 2
            return results[0] - results[1]
        elif tree._op == operators.Operation.mul:
            return results[0] * results[1]
        elif tree._op == operators.Operation.evaluate:
            return results[0].func(results[1:])
        elif tree._op == operators.Operation.div:
            return results[0] / results[1]

        else:
            raise ValueError("Should not happen")


class EquationManager:
    def __init__(self, gb, equations: Optional[List[Equation]] = None) -> None:
        self.gb = gb
        self._set_variables(gb)

        if equations is None:
            self._equations = []
        else:
            self._equations = equations
            # Separate a dof-manager from assembler?
        self._assembler = pp.Assembler(gb)

    def _set_variables(self, gb):
        # Define variables as specified in the GridBucket
        variables = {}
        for g, d in gb:
            variables[g] = {}
            for var, info in d[pp.PRIMARY_VARIABLES].items():
                variables[g][var] = operators.Variable(var, info, g)

        for e, d in gb.edges():
            variables[e] = {}
            num_cells = d["mortar_grid"].num_cells
            for var, info in d[pp.PRIMARY_VARIABLES].items():
                variables[e][var] = operators.Variable(var, info, e, num_cells)

        self.variables = variables
        # Define discretizations

    def merge_variables(self, grid_var: List[Tuple[grid_like_type, str]]):
        return pp.ad.MergedVariable([self.variables[g][v] for g, v in grid_var])

    def variable_state(
        self, grid_var: List[Tuple[pp.Grid, str]], state: np.ndarray
    ) -> List[np.ndarray]:
        # This should likely be placed somewhere else
        values: List[np.ndarray] = []
        for item in grid_var:
            ind: np.ndarray = self._assembler.dof_ind(*item)
            values.append(state[ind])

        return values

    def assemble_matrix_rhs(self, state):
        mat: List[sps.spmatrix] = []
        b: List[np.ndarray] = []

        num_global_dofs = self._assembler.full_dof.sum()

        for eq in self._equations:
            ad = eq.to_ad(self._assembler, self.gb, state)

            # The columns of the Jacobian has the size of the local variables.
            # Map these to the global ones
            local_dofs = eq.local_dofs()
            num_local_dofs = local_dofs.size
            projection = sps.coo_matrix(
                (np.ones(num_local_dofs), (np.arange(num_local_dofs), local_dofs)),
                shape=(num_local_dofs, num_global_dofs),
            )

            mat.append(ad.jac * projection)

            # Concatenate the residuals
            # Multiply by -1 to move to the rhs
            b.append(-ad.val)

        A = sps.bmat([[m] for m in mat]).tocsr()
        rhs = np.hstack([vec for vec in b])
        return A, rhs

    def discretize(self):
        # Somehow loop over all equations, discretize identified objects
        # (but should also be able to do rediscretization based on
        # dependency graph etc).
        pass