""" Implementation of a degree of freedom manager. 
"""
from typing import Dict, Tuple, Union, List

import numpy as np
import porepy as pp


class DofManager:
    """
    block_dof: Is a dictionary with keys that are either
        Tuple[pp.Grid, variable_name: str] for nodes in the GridBucket, or
        Tuple[Tuple[pp.Grid, pp.Grid], str] for edges in the GridBucket.

        The values in block_dof are integers 0, 1, ..., that identify the block
        index of this specific grid (or edge) - variable combination.

    full_dof: Is a np.ndarray of int that store the number of degrees of
        freedom per key-item pair in block_dof. Thus
          len(full_dof) == len(block_dof).
        The total size of the global system is full_dof.sum()

    """

    def __init__(self, gb):

        # Counter for block index
        block_dof_counter = 0

        # Dictionary that maps node/edge + variable combination to an index.
        block_dof: Dict[Tuple[Union[pp.Grid, Tuple[pp.Grid, pp.Grid]], str], int] = {}

        # Storage for number of dofs per variable per node/edge, with respect
        # to the ordering specified in block_dof
        full_dof: List[int] = []

        for g, d in gb:
            if pp.PRIMARY_VARIABLES not in d:
                continue

            for local_var, local_dofs in d[pp.PRIMARY_VARIABLES].items():
                # First assign a block index.
                # Note that the keys in the dictionary is a tuple, with a grid
                # and a variable name (str)
                block_dof[(g, local_var)] = block_dof_counter
                block_dof_counter += 1

                # Count number of dofs for this variable on this grid and store it.
                # The number of dofs for each grid entitiy type defaults to zero.
                total_local_dofs = (
                    g.num_cells * local_dofs.get("cells", 0)
                    + g.num_faces * local_dofs.get("faces", 0)
                    + g.num_nodes * local_dofs.get("nodes", 0)
                )
                full_dof.append(total_local_dofs)

        for e, d in gb.edges():
            if pp.PRIMARY_VARIABLES not in d:
                continue

            mg: pp.MortarGrid = d["mortar_grid"]

            for local_var, local_dofs in d[pp.PRIMARY_VARIABLES].items():

                # First count the number of dofs per variable. Note that the
                # identifier here is a tuple of the edge and a variable str.
                block_dof[(e, local_var)] = block_dof_counter
                block_dof_counter += 1

                # We only allow for cell variables on the mortar grid.
                # This will not change in the foreseeable future
                total_local_dofs = mg.num_cells * local_dofs.get("cells", 0)
                full_dof.append(total_local_dofs)

        # Array version of the number of dofs per node/edge and variable
        self.full_dof: np.ndarray = np.array(full_dof)
        self.block_dof: Dict[
            Tuple[Union[pp.Grid, Tuple[pp.Grid, pp.Grid]], str], int
        ] = block_dof

    def dof_ind(
        self, g: Union[pp.Grid, Tuple[pp.Grid, pp.Grid]], name: str
    ) -> np.ndarray:
        """Get the indices in the global system of variables associated with a
        given node / edge (in the GridBucket sense) and a given variable.

        Parameters:
            g (pp.Grid or pp.GridBucket edge): Either a grid, or an edge in the
                GridBucket.
            name (str): Name of a variable. Should be an active variable.

        Returns:
            np.array (int): Index of degrees of freedom for this variable.

        """
        block_ind = self.block_dof[(g, name)]
        dof_start = np.hstack((0, np.cumsum(self.full_dof)))
        return np.arange(dof_start[block_ind], dof_start[block_ind + 1])

    def __str__(self) -> str:
        grid_likes = [key[0] for key in self.block_dof]
        unique_grids = list(set(grid_likes))

        num_grids = 0
        num_interfaces = 0
        for g in unique_grids:
            if isinstance(g, pp.Grid):
                num_grids += 1
            else:
                num_interfaces += 1

        names = [key[1] for key in self.block_dof]
        unique_vars = list(set(names))
        s = (
            f"Degree of freedom manager for {num_grids} "
            f"subdomains and {num_interfaces} interfaces.\n"
            f"Total number of degrees of freedom: {self.num_dof()}\n"
            "Total number of subdomain and interface variables:"
            f"{len(self.block_dof)}\n"
            f"Variable names: {unique_vars}"
        )

        return s

    def __repr__(self) -> str:

        grid_likes = [key[0] for key in self.block_dof]
        unique_grids = list(set(grid_likes))

        num_grids = 0
        num_interfaces = 0

        dim_max = -1
        dim_min = 4

        for g in unique_grids:
            if isinstance(g, pp.Grid):
                num_grids += 1
                dim_max = max(dim_max, g.dim)
                dim_min = min(dim_min, g.dim)
            else:
                num_interfaces += 1

        s = (
            f"Degree of freedom manager with in total {self.full_dof.sum()} dofs"
            f" on {num_grids} subdomains and {num_interfaces} interface variables.\n"
            f"Maximum grid dimension: {dim_max}\n"
            f"Minimum grid dimension: {dim_min}\n"
        )

        return s