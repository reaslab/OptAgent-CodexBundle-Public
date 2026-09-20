# 4x4x4 Cooperative 3D Tic-Tac-Toe Optimization

In a 4x4x4 three-dimensional tic-tac-toe board, players alternately place X
and O symbols. The game continues until all 64 cells are filled. The stated
game-theoretic objective is cooperative: choose the final assignment of X/O
symbols so that the total number of complete straight lines (rows, columns,
and diagonals of length 4, in every valid 3D direction) is minimized,
regardless of whether a line consists of X or O.

Model the final board state directly. Every cell must contain exactly one of X
or O. A complete line is counted when all four cells on that line have the same
symbol. Minimize the total count of complete monochromatic lines.

The alternating move order is relevant to reachability, but no intermediate
move restrictions or adversarial payoffs are imposed in this cooperative final
state formulation. State this assumption explicitly and provide a verifiable
MILP solution.
