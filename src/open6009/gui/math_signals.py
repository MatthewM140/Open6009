"""Restricted, unit-aware expressions over aligned analog scan values."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from math import isfinite, nan


@dataclass(frozen=True)
class MathProgram:
    expression: str
    dependencies: tuple[str, ...]
    unit: str
    _tree: ast.Expression
    _symbols: dict[str, str]

    def evaluate(self, values: dict[str, float]) -> float:
        def visit(node: ast.AST) -> float:
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return values[self._symbols[node.id]]
            if isinstance(node, ast.UnaryOp):
                result = visit(node.operand)
                return -result if isinstance(node.op, ast.USub) else result
            if isinstance(node, ast.BinOp):
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right if right != 0 else nan
            if isinstance(node, ast.Call):
                arguments = [visit(arg) for arg in node.args]
                if node.func.id == "V":
                    return arguments[0]
                if node.func.id == "abs":
                    return abs(arguments[0])
                if node.func.id == "min":
                    return min(arguments)
                if node.func.id == "max":
                    return max(arguments)
                if node.func.id == "clamp":
                    return min(max(arguments[0], arguments[1]), arguments[2])
            raise ValueError("unsupported math operation")

        return visit(self._tree.body)


def compile_math(expression: str, symbols: dict[str, tuple[str, str]]) -> MathProgram:
    """Compile only allowlisted arithmetic. Symbols map stable handles to IDs and units."""

    if len(expression) > 256:
        raise ValueError("math expression exceeds 256 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid math expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 96:
        raise ValueError("math expression is too complex")
    dependencies: list[str] = []

    def unit_of(node: ast.AST, depth: int = 0) -> str:
        if depth > 20:
            raise ValueError("math expression is too deeply nested")
        if (isinstance(node, ast.Constant) and type(node.value) in (int, float)
                and isfinite(node.value)):
            return "1"
        if isinstance(node, ast.Name):
            if node.id not in symbols:
                raise ValueError(f"unknown math symbol {node.id}")
            identifier, unit = symbols[node.id]
            if identifier not in dependencies:
                dependencies.append(identifier)
            return unit
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return unit_of(node.operand, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left = unit_of(node.left, depth + 1)
            right = unit_of(node.right, depth + 1)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if left != right:
                    raise ValueError("addition and subtraction require matching units")
                return left
            if isinstance(node.op, ast.Mult):
                if left != "1" and right != "1":
                    raise ValueError("multiplication of two dimensional signals is unsupported")
                return right if left == "1" else left
            if right == "1":
                return left
            if left == right:
                return "1"
            raise ValueError("division by a dimensional signal is unsupported")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and not node.keywords and node.func.id in {"V", "abs", "min", "max", "clamp"}):
            name = node.func.id
            if name in {"V", "abs"} and len(node.args) != 1:
                raise ValueError(f"{name} requires one argument")
            if name == "clamp" and len(node.args) != 3:
                raise ValueError("clamp requires three arguments")
            if name in {"min", "max"} and not 1 <= len(node.args) <= 8:
                raise ValueError(f"{name} requires 1..8 arguments")
            units = [unit_of(arg, depth + 1) for arg in node.args]
            if name == "V":
                if units[0] != "1":
                    raise ValueError("V() requires a dimensionless constant or expression")
                return "V"
            if len(set(units)) != 1:
                raise ValueError(f"{name} requires matching units")
            return units[0]
        raise ValueError("unsupported math syntax")

    unit = unit_of(tree.body)
    return MathProgram(expression, tuple(dependencies), unit, tree,
                       {name: identifier for name, (identifier, _) in symbols.items()})
