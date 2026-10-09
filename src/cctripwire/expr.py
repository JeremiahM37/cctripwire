"""A tiny, safe expression language for policy rules.

Supports: and/or/not, comparisons (chained), + - * /, numbers, strings, True/False/None,
names and attribute access (five_hour.used_pct). No calls, subscripts or lambdas.
Unknown values are None, and any comparison or arithmetic involving None is False/None,
so a rule never fires on missing data.
"""
from __future__ import annotations

import ast
import operator
from functools import lru_cache
from typing import Any

_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
            ast.BinOp, ast.Name, ast.Attribute, ast.Constant, ast.Load, ast.Is, ast.IsNot,
            *tuple(_CMP), *tuple(_BIN))


@lru_cache(maxsize=256)
def compile_expr(src: str) -> ast.Expression:
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed")
    return tree


def _get(obj: Any, attr: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def _ev(node: ast.AST, env: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _ev(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute):
        return _get(_ev(node.value, env), node.attr)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_ev(v, env)) for v in node.values)
        return any(bool(_ev(v, env)) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not v
        return None if v is None else -v
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, env), _ev(node.right, env)
        if a is None or b is None:
            return None
        try:
            return _BIN[type(node.op)](a, b)
        except ZeroDivisionError:
            return None
    if isinstance(node, ast.Compare):
        left = _ev(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, env)
            if isinstance(op, ast.Is):
                ok = left is right
            elif isinstance(op, ast.IsNot):
                ok = left is not right
            elif left is None or right is None:
                return False
            else:
                try:
                    ok = _CMP[type(op)](left, right)
                except TypeError:
                    return False
            if not ok:
                return False
            left = right
        return True
    raise ValueError(f"unsupported node {type(node).__name__}")


def evaluate(src: str, env: dict) -> Any:
    return _ev(compile_expr(src), env)
